"""Live nucbox dispatcher probes over ssh, with a fixed command allowlist.

The dispatcher, watchdog and workers run on ``nucbox-wsl`` and are owned by
``maintainer/nucbox-jobs`` (``docs/ops/nucbox-fleet.md``). Nothing
here edits them; these are the same read-only one-liners the skill's "Ops
quick reference" tells a human to run.

There is deliberately no "run this command on nucbox" tool. Every remote
command is a literal in :data:`PROBES`, so the only caller-supplied value that
reaches the remote shell is a line count this module casts to ``int`` and
clamps.

An unreachable box reports UNKNOWN per probe and the aggregate refuses to
summarize into "ok" -- absence of a failure signal is not health.
"""

from __future__ import annotations

from typing import Any

from apps.fleet_mcp.config import (
    DEFAULT_LOG_LINES,
    MAX_LOG_LINES,
    NUCBOX_SSH_HOST,
    SSH_CONNECT_TIMEOUT_S,
    SSH_TIMEOUT_S,
    clamp,
)
from apps.fleet_mcp.runner import UNKNOWN, Runner, run

#: Remote command per probe name. Literals only -- see the module docstring.
PROBES: dict[str, str] = {
    "pressure": "~/jobs/pressure.sh",
    "kpi": "~/jobs/kpi.sh",
    "watchdog": "systemctl --user is-active queue-watchdog.service",
    "workers": "tmux ls",
}

#: Probes whose one-line output is the fleet's spawn gate.
HEALTH_PROBES: tuple[str, ...] = ("pressure", "watchdog", "workers")

#: The statuses that count as a HEALTHY measurement for each gate probe. The
#: aggregate says "ok" only when every probe lands in its own set, so a status
#: nobody anticipated fails closed rather than passing silently.
#: ``pressure`` keeps "high" here because a high-pressure reading is a working
#: probe reporting a closed gate, which `health` surfaces as its own verdict.
HEALTHY_STATUSES: dict[str, frozenset[str]] = {
    "pressure": frozenset({"ok", "high"}),
    "watchdog": frozenset({"measured"}),
    "workers": frozenset({"measured"}),
}

_SSH_BASE = (
    "ssh",
    "-o",
    "BatchMode=yes",
    "-o",
    f"ConnectTimeout={SSH_CONNECT_TIMEOUT_S}",
    NUCBOX_SSH_HOST,
)


def _ssh(remote_command: str, runner: Runner) -> dict[str, Any]:
    completed = runner([*_SSH_BASE, remote_command], timeout=SSH_TIMEOUT_S)
    if not completed.measured:
        return {
            "status": UNKNOWN,
            "reason": completed.unknown_reason,
            "host": NUCBOX_SSH_HOST,
        }
    if not completed.ok:
        # ssh exits 255 for a transport failure; the remote command's own
        # nonzero exit is a measurement, not an unreachable box.
        if completed.returncode == 255:
            return {
                "status": UNKNOWN,
                "reason": f"ssh {NUCBOX_SSH_HOST} failed: {completed.stderr.strip()[:300]}",
                "host": NUCBOX_SSH_HOST,
            }
        return {
            "status": "error",
            "returncode": completed.returncode,
            "stdout": completed.stdout.strip()[:1000],
            "stderr": completed.stderr.strip()[:500],
        }
    return {
        "status": "measured",
        "stdout": completed.stdout.strip()[:4000],
        "stderr": completed.stderr.strip()[:500],
    }


def _pressure_verdict(document: dict[str, Any]) -> dict[str, Any]:
    """Read ``PRESSURE ok|high`` out of the probe's own line, or say UNKNOWN.

    The line is the fleet's spawn gate. A probe that ran but printed something
    this parser does not recognize is UNKNOWN, not ok: an unparsed line is a
    failed measurement, and rendering it as a pass is the defect the
    verification rule names.
    """
    if document["status"] != "measured":
        return document
    line = document["stdout"].splitlines()[0] if document["stdout"] else ""
    tokens = line.split()
    verdict = tokens[1].lower() if len(tokens) >= 2 and tokens[0].upper() == "PRESSURE" else None
    if verdict not in {"ok", "high"}:
        return {
            "status": UNKNOWN,
            "reason": f"pressure.sh printed an unrecognized line: {line[:200]!r}",
            "line": line[:200],
        }
    return {"status": verdict, "line": line[:300], "detail": document["stdout"][:1500]}


def _worker_verdict(document: dict[str, Any]) -> dict[str, Any]:
    """Count live ``job-<N>`` tmux sessions.

    ``tmux ls`` exits nonzero with "no server running" when nothing is up,
    which is a real measurement of zero workers, not a failed probe.
    """
    if document["status"] == UNKNOWN:
        return document
    text = f"{document.get('stdout', '')}\n{document.get('stderr', '')}"
    if "no server running" in text:
        return {"status": "measured", "live_workers": 0, "sessions": []}
    if document["status"] != "measured":
        return document
    sessions = [
        line.split(":", 1)[0]
        for line in document["stdout"].splitlines()
        if line.startswith("job-")
    ]
    return {"status": "measured", "live_workers": len(sessions), "sessions": sessions}


def probe(name: str, *, runner: Runner = run) -> dict[str, Any]:
    """Run one allowlisted probe by name."""
    if name not in PROBES:
        raise ValueError(f"unknown probe {name!r}; allowed: {', '.join(sorted(PROBES))}")
    document = _ssh(PROBES[name], runner)
    if name == "pressure":
        return _pressure_verdict(document)
    if name == "workers":
        return _worker_verdict(document)
    return document


def health(*, runner: Runner = run) -> dict[str, Any]:
    """Aggregate the spawn-gate probes without inventing an overall verdict.

    [if] every gate probe reports one of its own HEALTHY statuses [then] the
    aggregate may say so, [else stop] -- an unmeasured probe reports UNKNOWN and
    a probe that ran and FAILED reports ``error``. Both refuse "ok".
    """
    probes = {name: probe(name, runner=runner) for name in HEALTH_PROBES}
    unmeasured = [name for name, document in probes.items() if document["status"] == UNKNOWN]
    # Presence of a healthy measurement, not absence of UNKNOWN. `systemctl
    # is-active` exits nonzero for an INACTIVE watchdog, which _ssh reports as
    # `error`; keying the gate on UNKNOWN alone published `ok` for a dead
    # watchdog and sent callers to spawn into an unhealthy fleet (Codex P1,
    # #3735). Anything not affirmatively healthy is not health.
    failed = [
        name
        for name, document in probes.items()
        if document["status"] != UNKNOWN and document["status"] not in HEALTHY_STATUSES[name]
    ]
    if unmeasured:
        overall = UNKNOWN
    elif failed:
        overall = "error"
    elif probes["pressure"]["status"] == "high":
        overall = "high"
    else:
        overall = "ok"
    document: dict[str, Any] = {
        "host": NUCBOX_SSH_HOST,
        "status": overall,
        "probes": probes,
        "owner_repo": "maintainer/nucbox-jobs",
    }
    if unmeasured:
        document["unmeasured"] = unmeasured
        document["reason"] = (
            f"{', '.join(unmeasured)} could not be measured; no health verdict is claimed"
        )
    if failed:
        document["failed"] = failed
        document["reason"] = (
            f"{', '.join(failed)} ran and reported a failure; the spawn gate is not ok"
        )
    return document


def dispatcher_log(lines: int | None = None, *, runner: Runner = run) -> dict[str, Any]:
    """Tail the dispatcher's tick transcript."""
    count = clamp(lines, DEFAULT_LOG_LINES, MAX_LOG_LINES)
    document = _ssh(f"tail -n {count} ~/jobs/logs/dispatcher.log", runner)
    document["lines_requested"] = count
    document["path"] = "~/jobs/logs/dispatcher.log"
    return document
