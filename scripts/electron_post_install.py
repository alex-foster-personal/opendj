"""After the Electron install: acceptance, relaunch, engine health, running stamp (OPS-58 for OPS-63).

``scripts/ship_electron.py`` installs the packed Electron app. This module is
what a Tauri rollout does next (``ship_dmg.sh``: ``_m_run_acceptance``,
``_m_relaunch_app``, ``_m_health_report``), so an Electron build earns the
word SHIPPED the same way a Tauri dmg does. Stdlib only and Python 3.9
compatible, like ship_electron.py. ADR:
(private path).

Order per installed machine, as in ship_dmg.sh: the installed-app acceptance
(only on an acceptance host, while the app is closed), then the scrubbed
relaunch; then, once every machine is relaunched, the health phase. A failed
acceptance or an unhealthy engine never stops the later steps: the run
finishes them and exits nonzero, "installed but NOT shipped".

Requirements:

- ✔︎ acceptance: ``acceptance.sh`` (the same script, fixture and checks as
  the Tauri path; app_acceptance.py detects the Electron bundle) runs on
  demon-llama by default, on the Air with ``--acceptance-on air``, never on
  silver. An install with no acceptance anywhere is NOT accepted unless
  ``--no-acceptance`` says so.
  - [if] acceptance.sh exits nonzero [then ⛔️] the run exits 1 naming the
    failed checks from the result JSON
  - [if] the run installs only on machines that are not acceptance hosts and
    ``--no-acceptance`` is absent [then ⛔️] the run exits 1 as NOT accepted
  - [if] ``--acceptance-on silver`` is given [then ⛔️] exit 2 before anything runs
- ✔︎ relaunch: ``env -i HOME USER PATH AF_SERVICE_ID open -a`` (OPS-32), so the
  operator's Doppler secrets never reach the launched app.
- ✔︎ health: the engine of THIS bundle (its payload runtime's listening port)
  answers /api/v1/health with a status and a state_db, reports no
  forbidden env key with HOME present as the positive control, and
  /api/v1/build-info reports source=payload at the target sha.
  - [if] the engine reports a forbidden env key, or HOME absent [then ⛔️] UNHEALTHY
  - [if] build-info names another sha or source=repo [then ⛔️] UNHEALTHY
  - [if] no engine of this bundle answers within HEALTH_WAIT_S [then ⛔️] UNHEALTHY
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ----- CFG ---------------------------------------------------------------
ACCEPTANCE_SH_REL = "(private path)"
# The same lists as ship_dmg.sh (OPS-58); tests pin them equal.
ACCEPTANCE_DEFAULT_MACHINES = ("demon-llama",)
ACCEPTANCE_REFUSED_MACHINES = ("silver",)
ACCEPTANCE_ELIGIBLE_MACHINES = ("demon-llama", "air")
HEALTH_WAIT_S = 90
HEALTH_POLL_S = 3
# ship_dmg.sh FORBIDDEN_ENV_PREFIXES (OPS-32); tests pin them equal.
FORBIDDEN_ENV_PREFIXES = ("R2_", "CF_R2_", "TAURI_SIGNING_", "DOPPLER_")
RELAUNCH_SERVICE_ID = "com.af.music-dj-tools.desktop"
# Absolute, so the bash that runs acceptance.sh is never a PATH lookup (ship_dmg.sh parity).
KNOWN_BASHES = ("/bin/bash", "/usr/bin/bash")
PROBE_NO_ENGINE_EXIT = 3
PROBE_NO_PORT_EXIT = 4


@dataclass
class Outcome:
    """What the post-install phase found. Nothing here stops a later step."""

    acceptance_notes: list[str] = field(default_factory=list)
    acceptance_failed: bool = False
    acceptance_ran: bool = False
    unhealthy: list[str] = field(default_factory=list)

    def failures(self, *, installed: bool, no_acceptance: bool) -> list[str]:
        """Every reason the run is NOT shipped; empty when it is."""
        reasons: list[str] = []
        if self.unhealthy:
            reasons.append(f"post-install health failed on {', '.join(self.unhealthy)}")
        if installed and not no_acceptance and not self.acceptance_ran:
            reasons.append(
                "NOT-RUN: no acceptance machine was installed (include demon-llama, "
                "add --acceptance-on air, or pass --no-acceptance)"
            )
        if self.acceptance_failed:
            reasons.append(f"ACCEPTANCE FAILED: {' '.join(self.acceptance_notes)}")
        return reasons


# ----- acceptance --------------------------------------------------------


def acceptance_refusal(extra: Sequence[str]) -> str | None:
    """Why an --acceptance-on value is refused, or None (ship_dmg.sh's checks, same words)."""
    for machine in extra:
        if machine in ACCEPTANCE_REFUSED_MACHINES:
            return f"[ERROR] --acceptance-on {machine} is refused: the acceptance never runs on {machine} (OPS-58)"
        if machine not in ACCEPTANCE_ELIGIBLE_MACHINES:
            return (
                f"[ERROR] --acceptance-on {machine}: not an acceptance host "
                f"(eligible: {' '.join(ACCEPTANCE_ELIGIBLE_MACHINES)})"
            )
    return None


def acceptance_wanted(machine: str, extra: Sequence[str], no_acceptance: bool) -> bool:
    if no_acceptance or machine in ACCEPTANCE_REFUSED_MACHINES:
        return False
    return machine in ACCEPTANCE_DEFAULT_MACHINES or machine in extra


def known_bash() -> str:
    for candidate in KNOWN_BASHES:
        if Path(candidate).is_file():
            return candidate
    raise FileNotFoundError(f"no known bash at {' or '.join(KNOWN_BASHES)}")


def acceptance_argv(
    repo_root: Path, bash: str, machine: str, local: str, ssh_host: str, app: str, sha: str, result: Path
) -> list[str]:
    target = ["--machine", "here"] if machine == local else ["--machine", machine, "--ssh-host", ssh_host]
    return [
        bash,
        str(repo_root / ACCEPTANCE_SH_REL),
        *target,
        "--app",
        app,
        "--expect-sha",
        sha,
        "--result",
        str(result),
    ]


def failed_checks(result: Path) -> list[str] | None:
    """The result's failed_checks, or None when the result cannot be read."""
    try:
        body = json.loads(result.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(body, dict):
        return None
    if body.get("overall") == "REFUSED":
        return [f"REFUSED: {body.get('refused')}"]
    checks = body.get("failed_checks")
    return [str(c) for c in checks] if isinstance(checks, list) else None


def record_acceptance(outcome: Outcome, machine: str, returncode: int, result: Path) -> str:
    """Fold one acceptance.sh exit into the outcome; return the note."""
    outcome.acceptance_ran = True
    if returncode == 0:
        note = f"{machine}=PASS"
    else:
        outcome.acceptance_failed = True
        failed = failed_checks(result)
        detail = ",".join(failed) if failed else f"rc {returncode}, see {result}"
        note = f"{machine}=FAIL({detail})"
    outcome.acceptance_notes.append(note)
    return note


# ----- relaunch ----------------------------------------------------------


def relaunch_command(bundle: str) -> str:
    """ship_dmg.sh _m_relaunch_app: `open` forwards its own env, so env -i scrubs it (OPS-32)."""
    return (
        'env -i HOME="$HOME" USER="$USER" PATH=/usr/bin:/bin:/usr/sbin:/sbin '
        f"AF_SERVICE_ID={RELAUNCH_SERVICE_ID} open -a {shlex.quote(bundle)}"
    )


# ----- health ------------------------------------------------------------


def engine_probe_command(bundle: str) -> str:
    """Find THIS bundle's engine (its payload runtime, as ship_dmg.sh _m_engine_port
    does), then print its port, /health body and /build-info body on three
    marked lines. Exit 3: no engine process of this bundle; 4: it has no
    listening port yet."""
    runtime = shlex.quote(f"{bundle}/Contents/Resources/payload/runtime/bin/")
    return (
        f"pid=$(ps -axo pid=,comm= | grep -F {runtime} | awk '{{print $1}}' | head -1); "
        f'[ -n "$pid" ] || exit {PROBE_NO_ENGINE_EXIT}; '
        'port=$(lsof -a -p "$pid" -iTCP -sTCP:LISTEN -nP -Fn 2>/dev/null '
        "| sed -n 's/^n.*:\\([0-9][0-9]*\\)$/\\1/p' | head -1); "
        f'[ -n "$port" ] || exit {PROBE_NO_PORT_EXIT}; '
        'echo "PORT $port"; '
        'printf "HEALTH "; curl -sS -m 5 "http://127.0.0.1:$port/api/v1/health" | tr -d "\\n"; echo; '
        'printf "BUILD "; curl -sS -m 5 "http://127.0.0.1:$port/api/v1/build-info" | tr -d "\\n"; echo'
    )


@dataclass
class Probe:
    port: int | None
    health: Any
    build_info: Any


def parse_probe(stdout: str) -> Probe:
    port: int | None = None
    bodies: dict[str, Any] = {"HEALTH": None, "BUILD": None}
    for line in stdout.splitlines():
        tag, _, rest = line.partition(" ")
        if tag == "PORT" and rest.strip().isdigit():
            port = int(rest.strip())
        elif tag in bodies:
            try:
                bodies[tag] = json.loads(rest)
            except ValueError:
                bodies[tag] = None
    return Probe(port, bodies["HEALTH"], bodies["BUILD"])


def health_problems(probe: Probe, target_sha: str) -> list[str]:
    """Why this engine is not a healthy, clean, target build; [] when it is. Pure."""
    health, build = probe.health, probe.build_info
    if not isinstance(health, dict):
        return ["/api/v1/health returned no JSON object"]
    problems: list[str] = []
    if not isinstance(health.get("status"), str) or not isinstance(health.get("state_db"), dict):
        problems.append("health body has no status or state_db")
    forbidden = health.get("process_env_forbidden_keys")
    if not isinstance(forbidden, list) or health.get("process_env_home_present") is not True:
        problems.append("engine environment UNKNOWN: no process_env_forbidden_keys list or HOME not reported present")
    else:
        leaked = [k for k in forbidden if isinstance(k, str) and k.startswith(FORBIDDEN_ENV_PREFIXES)]
        if leaked:
            problems.append(f"engine environment LEAKS operator secrets: {' '.join(leaked)}")
    if not isinstance(build, dict):
        problems.append("/api/v1/build-info returned no JSON object: running build UNVERIFIED")
    elif build.get("source") != "payload" or build.get("git_sha_full") != target_sha:
        problems.append(
            f"running build is source={build.get('source')!r} sha={str(build.get('git_sha_full'))[:12]!r}, "
            f"not the installed payload at {target_sha[:12]}"
        )
    return problems


def health_summary(probe: Probe) -> str:
    health: dict[str, Any] = probe.health if isinstance(probe.health, dict) else {}
    raw_state = health.get("state_db")
    state: dict[str, Any] = raw_state if isinstance(raw_state, dict) else {}
    return (
        f"engine on port {probe.port}, status={health.get('status')}, "
        f"tracks={state.get('tracks')}, playlists={state.get('playlists')}"
    )
