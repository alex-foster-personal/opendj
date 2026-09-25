"""Data model, host list, and ownership rules for the fleet process registry.

This module holds the two things that are cheap to keep correct by hand:

* ``HOSTS`` -- which machines to ask, and how (launchd / systemd-user /
  systemd-root / crontab / Windows Task Scheduler / GitHub Actions).
* ``OWNERSHIP_RULES`` -- a small, regex-keyed table saying which of those
  units are ours (area + one-line purpose), because "do we own this" is a
  judgment call no probe can make from a name alone.

Everything else (is it enabled, is it running, when did it last run) is
NEVER hand-maintained here -- ``scripts/process_registry_gen.py`` and
``scripts/process_registry_check.py`` re-derive it live every time, per
``.claude/rules/verification.md``: prefer an invariant to a value, a
procedure to a verdict.

Naming convention under audit (issue #2542): ``opendj-<area>-<part>`` for
systemd units and process titles, ``com.opendj.<area>-<part>`` for launchd
labels.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from enum import StrEnum

from scripts.oss_tip_rules import PATTERNS, _rule_accepts


class SchedulerKind(StrEnum):
    LAUNCHD = "launchd"
    SYSTEMD_USER = "systemd-user"
    SYSTEMD_SYSTEM = "systemd-system"
    CRON = "cron"
    WINDOWS_TASK = "windows-task-scheduler"
    GITHUB_ACTIONS = "github-actions"


class HostUnreachableError(RuntimeError):
    """Raised when a host cannot be queried at all.

    The check script MUST treat this as UNKNOWN, never as "zero units,
    therefore clean" -- that is the exact absence-of-a-bad-thing trap
    ``.claude/rules/verification.md`` warns about.
    """


@dataclass(frozen=True)
class Host:
    name: str
    scheduler: SchedulerKind
    description: str
    # None => local (no ssh hop). Non-None => the ssh alias to reach it.
    ssh_alias: str | None = None


# Hosts in scope for Part 1 of issue #2542. Order matches the issue's list.
HOSTS: list[Host] = [
    Host("silver", SchedulerKind.LAUNCHD, "this Mac (local), primary lanes checkout", None),
    Host("air", SchedulerKind.LAUNCHD, "the Air, interactive strategist + fleet-dash", "air"),
    Host("demon-llama", SchedulerKind.LAUNCHD, "demon-llama Mac", "demon-llama"),
    Host("nucbox-wsl", SchedulerKind.SYSTEMD_USER, "nucbox Ubuntu/WSL2 build fleet", "nucbox-wsl"),
    Host("agentbox", SchedulerKind.SYSTEMD_SYSTEM, "Hetzner agentbox execution plane", "agentbox"),
    Host("bifrost2", SchedulerKind.WINDOWS_TASK, "bifrost2 Windows PC", "bifrost2"),
]

GITHUB_ACTIONS_HOST = Host(
    "github-actions", SchedulerKind.GITHUB_ACTIONS, "music-dj-tools .github/workflows", None
)

# ---------------------------------------------------------------- naming

# The convention is scheduler-shaped: launchd labels are reverse-DNS style,
# systemd units and process titles are dash-joined. Applying one regex to
# both would flag every correct systemd unit as a naming violation, so the
# check is keyed by scheduler kind. GitHub Actions workflows are not
# processes and are exempt (N/A), never silently marked "violation".
_LAUNCHD_NS_RE = re.compile(r"^com\.opendj\.")
_SYSTEMD_NS_RE = re.compile(r"^opendj-")
_WINDOWS_NS_RE = re.compile(r"^opendj-", re.IGNORECASE)
# macOS synthesizes "application.<bundle-id>.<pid>.<pid>" launchd keys for
# running app instances; that shape is not one we choose, so it is exempt
# rather than flagged.
_MACOS_APP_INSTANCE_RE = re.compile(r"^application\.")


def naming_status(scheduler: SchedulerKind, unit_name: str) -> str:
    """Return 'ok', 'violation', or 'n/a' for the namespace convention."""
    if scheduler == SchedulerKind.GITHUB_ACTIONS:
        return "n/a"
    if scheduler == SchedulerKind.LAUNCHD:
        if _MACOS_APP_INSTANCE_RE.match(unit_name):
            return "n/a"
        return "ok" if _LAUNCHD_NS_RE.match(unit_name) else "violation"
    if scheduler == SchedulerKind.WINDOWS_TASK:
        return "ok" if _WINDOWS_NS_RE.match(unit_name) else "violation"
    # systemd-user, systemd-system, cron: process/unit title convention.
    return "ok" if _SYSTEMD_NS_RE.match(unit_name) else "violation"


# ------------------------------------------------------------- ownership


@dataclass(frozen=True)
class OwnershipRule:
    host_pattern: str  # regex against Host.name, "*" matches all
    unit_pattern: str  # regex against the unit/label/task name
    area: str
    purpose: str
    owned: bool = True


# Ordered: first match wins. Keep entries small and specific; this table is
# the one part of the registry that is legitimately hand-maintained, because
# "do we own this and why" cannot be derived from a name alone. Everything
# NOT matched here falls through to a per-host default (see
# ``DEFAULT_OWNED_HOSTS`` / ``_default_for_host``).
OWNERSHIP_RULES: list[OwnershipRule] = [
    # --- Mac / launchd: opendj product processes -----------------------
    OwnershipRule(
        "*", r"^com\.opendj\.host-disk-mac$", "host", "Mac memory/disk watchdog for the fleet"
    ),
    OwnershipRule(
        "*", r"^com\.opendj\.performance-probe$", "perf", "opendj preview-engine performance probe"
    ),
    OwnershipRule("*", r"^com\.opendj\.ci-wt-prune$", "ci", "prunes stale IDD worktrees"),
    OwnershipRule(
        "*",
        r"^com\.opendj\.sweep-workflow-finisher$",
        "ci",
        "replays workflow-file PR merges nucbox's gh token can't push (missing workflow scope)",
    ),
    OwnershipRule("*", r"^com\.opendj\.build-once$", "build", "one-shot opendj build trigger"),
    OwnershipRule(
        "*",
        r"^opendj-hostcleanup-artifactcap$",
        "host",
        "prunes old GitHub Actions artifacts locally (launchd label predates the "
        "com.opendj. convention)",
    ),
    OwnershipRule(
        "*",
        r"^opendj-hostcleanup-wtprune$",
        "host",
        "prunes stale IDD worktrees on the Air (launchd label predates the com.opendj. convention)",
    ),
    OwnershipRule(
        "*",
        r"^opendj-stems-push$",
        "stems",
        "pushes stem bundles to remote storage (launchd label predates the com.opendj. convention)",
    ),
    OwnershipRule(
        "*",
        r"^com\.af\.opendj-preview-(engine|vite|watch)$",
        "preview",
        "opendj live-review preview stack (engine/vite/watch)",
    ),
    OwnershipRule(
        "*",
        r"^com\.af\.autoreposync(\..+)?$",
        "repo-sync",
        "cross-machine music-dj-tools git repo sync daemon",
    ),
    # Supersedes: the fallback classification these three launchd labels got
    # before PR #3827 (owned=false, area n/a, "not matched to any
    # music-dj-tools ownership rule; presumed personal/third-party
    # automation"), which hid the repo's own dmg-smoke and perf-kpi agents
    # from this registry. They are now owned rows with a named area.
    OwnershipRule(
        "*",
        r"^com\.af\.dmg-smoke$",
        "build",
        "builds, launches and library-attach-smokes the signed desktop dmg on a cadence "
        "(DEVOPS-04, ops/dmg-smoke/)",
    ),
    OwnershipRule(
        "*",
        r"^com\.af\.perf-kpi-nightly$",
        "perf",
        "nightly deck-load perf KPI capture + ledger PR (DEVOPS-08, issue #1506)",
    ),
    OwnershipRule(
        "*",
        r"^com\.af\.perf-kpi-health$",
        "perf",
        "10-minute live preview-engine health probe + bounded restart (DEVOPS-08, issue #1506)",
    ),
    OwnershipRule(
        "*",
        r"^application\.com\.opendj\.desktop\.",
        "desktop-app",
        "running instance of the packaged Open DJ desktop app (Tauri bundle id com.opendj.desktop)",
    ),
    # --- nucbox-wsl: CI/build fleet (owned by nucbox-jobs repo, but
    # in scope here per issue #2542 acceptance criteria) ----------------
    OwnershipRule("nucbox-wsl", r"^idd-lane@", "idd", "IDD build lane worker instance"),
    OwnershipRule("nucbox-wsl", r"^idd-loadout-pick", "idd", "picks the next IDD issue loadout"),
    OwnershipRule(
        "nucbox-wsl", r"^idd-feed", "idd", "feeds parsed candidate issues to the IDD picker"
    ),
    OwnershipRule("nucbox-wsl", r"^idd-worktree-prune", "idd", "prunes leaked IDD worktrees"),
    OwnershipRule("nucbox-wsl", r"^idd-cost-annotate", "idd", "annotates IDD spend on issues/PRs"),
    OwnershipRule("nucbox-wsl", r"^sweep-handoff", "ci", "sweeper for orphaned/handed-off PRs"),
    OwnershipRule("nucbox-wsl", r"^artifact-gc", "ci", "garbage-collects old CI artifacts"),
    OwnershipRule("nucbox-wsl", r"^tmp-gc", "host", "reaps /tmp on nucbox-wsl"),
    OwnershipRule("nucbox-wsl", r"^basetemp-prune", "host", "prunes stale pytest basetemp dirs"),
    OwnershipRule("nucbox-wsl", r"^host-disk-(guard|sensor)", "host", "nucbox-wsl disk watchdog"),
    OwnershipRule(
        "nucbox-wsl",
        r"^trunk-(tip-only|checkpoint)",
        "ci",
        "trunk CI scheduling / checkpoint helper",
    ),
    OwnershipRule("nucbox-wsl", r"^harden-lane", "ci", "Codex hardening lane"),
    OwnershipRule(
        "nucbox-wsl",
        r"^gh-app-(refresh|health)",
        "ci",
        "GitHub App token refresh/health for the fleet's per-function Apps",
    ),
    OwnershipRule(
        "nucbox-wsl", r"^health-to-dispatch", "ci", "relays health signals into the dispatcher"
    ),
    OwnershipRule("nucbox-wsl", r"^ci-infra-tick", "ci", "CI-infra executor heartbeat tick"),
    OwnershipRule("nucbox-wsl", r"^ci-sentinel", "ci", "trunk-red sentinel"),
    OwnershipRule("nucbox-wsl", r"^ci-lane-scratch-reap", "ci", "reaps CI lane scratch state"),
    OwnershipRule(
        "nucbox-wsl", r"^runner-governor", "ci", "governs/drains self-hosted Actions runners"
    ),
    OwnershipRule("nucbox-wsl", r"^kpi-", "ci", "KPI scoring tick (trunk/merge/host/instrument)"),
    OwnershipRule("nucbox-wsl", r"^score-ci-lane", "ci", "CI lane KPI score tick"),
    OwnershipRule(
        "nucbox-wsl", r"^af-sub-broker-watchdog", "usage", "watchdog for the AI-subscription broker"
    ),
    OwnershipRule(
        "nucbox-wsl", r"^af-usage-attrib-refit", "usage", "reattributes fleet usage to accounts"
    ),
    OwnershipRule(
        "nucbox-wsl", r"^grok-burn-projection", "usage", "projects Grok subscription burn"
    ),
    OwnershipRule("nucbox-wsl", r"^usage-seats-watchdog", "usage", "watches account-seat usage"),
    OwnershipRule("nucbox-wsl", r"^scout-sweep", "ci", "hourly dispatch-machine inspection sweep"),
    OwnershipRule("nucbox-wsl", r"^opendj-sink-triage", "ops", "triages the opendj issue sink"),
    OwnershipRule(
        "nucbox-wsl",
        r"^opendj-runner-workspace-gc",
        "ci",
        "garbage-collects self-hosted runner workspaces",
    ),
    OwnershipRule(
        "nucbox-wsl", r"^preview-refresh", "preview", "refreshes the live-review preview branch"
    ),
    OwnershipRule("nucbox-wsl", r"^admin-platform-replay", "ops", "replays admin platform events"),
    OwnershipRule("nucbox-wsl", r"^recovery-health", "ci", "session-crash recovery health check"),
    # Stock distro/OS timers on nucbox-wsl -- explicitly not ours.
    OwnershipRule(
        "nucbox-wsl",
        r"^(git-maintenance@|wsl-fstrim|launchpadlib-cache-clean|ubuntu-insights-)",
        "n/a",
        "stock Ubuntu/WSL or git timer, not ours",
        owned=False,
    ),
    # --- agentbox: dedicated execution-plane infra ----------------------
    OwnershipRule(
        "agentbox",
        r"^agentbox-watchdog",
        "host",
        "silent-failure watchdog for the agentbox execution plane",
    ),
    OwnershipRule(
        "agentbox", r"^nucbox-liveness", "host", "cross-checks nucbox liveness from agentbox"
    ),
    OwnershipRule("agentbox", r"^boxwatch", "host", "agentbox self-health watch"),
    OwnershipRule("agentbox", r"^af-disk-watchdog", "host", "agentbox disk watchdog"),
    OwnershipRule(
        "agentbox",
        r"^codex-transcript-cleanup",
        "ops",
        "prunes old Codex transcripts on agentbox (root crontab, "
        "/root/.claude/scripts/codex-transcript-cleanup.sh)",
    ),
    # Stock Ubuntu timers on agentbox -- explicitly not ours.
    OwnershipRule(
        "agentbox",
        r"^(launchpadlib-cache-clean|sysstat-|idle-reboot|motd-news|dpkg-db-backup|logrotate|"
        r"update-notifier-|systemd-tmpfiles-clean|man-db|mdmonitor-oneshot|e2scrub_all|fstrim|"
        r"mdcheck_|apport-autoreport|snapd\.snap-repair|ua-timer)",
        "n/a",
        "stock Ubuntu system timer, not ours",
        owned=False,
    ),
    # --- GitHub Actions --------------------------------------------------
    OwnershipRule(
        "github-actions", r".*", "ci", "scheduled workflow in this repo's .github/workflows"
    ),
    # --- bifrost2: a legacy stems worker that IS product-related --------
    OwnershipRule(
        "bifrost2",
        r"^demucs-farm$",
        "stems",
        "legacy demucs stem-separation runner on bifrost2, Disabled; likely superseded "
        "by the Modal GPU farm",
    ),
]


def _default_for_host(host_name: str) -> tuple[bool, str, str]:
    """(owned, area, purpose) for a unit that matched no explicit rule."""
    if host_name in {"nucbox-wsl", "agentbox"}:
        # Dedicated build/agent infra: unmatched units are still plausibly
        # ours, but we did not have a confirmed purpose for them this pass.
        return True, "unclassified", "UNKNOWN: no ownership rule matched; not yet triaged"
    # Personal Macs / bifrost2 run a great deal of the maintainer's own automation and
    # third-party software that has nothing to do with music-dj-tools. An
    # unmatched unit there is treated as NOT ours by default (recorded in
    # the "not ours" section) rather than guessed at -- the 2.1.270 pattern
    # from issue #2542 itself.
    return (
        False,
        "n/a",
        "not matched to any music-dj-tools ownership rule; presumed personal/third-party "
        "automation, out of scope for this registry",
    )


def classify(host_name: str, unit_name: str) -> tuple[bool, str, str]:
    """Return (owned, area, purpose) for unit_name on host_name."""
    for rule in OWNERSHIP_RULES:
        if rule.host_pattern not in ("*", host_name):
            continue
        if re.search(rule.unit_pattern, unit_name):
            return rule.owned, rule.area, rule.purpose
    return _default_for_host(host_name)


# --------------------------------------------------------- identity scrub


# What each rule's match is replaced BY when a recorded value is normalized on its way
# into the artifact. The rule set itself is NOT restated here: `PATTERNS` and
# `_rule_accepts` come from `scripts/oss_tip_rules`, so a new identity shape is defined
# once and both the audit and this scrubber pick it up. Two scrubbers that disagreed
# would be worse than none, because the drift check below compares the two.
#
# Every placeholder is itself exempt from the rule it replaces, which is what makes the
# scrub idempotent: a second pass finds nothing left to replace, so re-running the
# generator over an already-normalized artifact cannot drift it. `/Users/dev` and
# `box.example-tailnet.ts.net` are the placeholders the repo already standardizes on;
# the `<>` forms are templates, which the audit exempts on FORM rather than by value.
_SCRUB_PLACEHOLDERS: dict[str, str] = {
    "home-path": "/Users/dev",
    "windows-home-path": "C:\\Users\\dev",
    "linux-home-path": "/home/dev",
    "consumer-mailbox": "<mailbox>@example.invalid",
    "tailnet-name": "box.example-tailnet.ts.net",
    "cgnat-address": "<cgnat-address>",
}


def scrub_identities(text: str) -> str:
    """Replace every string the going-public audit would report with a placeholder.

    The registry is a GENERATED, tracked artifact, and this module is what collects the
    values that go into it: a real home directory, mailbox or tailnet name in a unit
    name, a timer's activated unit or a launchd label on any queried host would be
    written straight into the repository and reported by `scripts.oss_tip_audit` on the
    next CI pass. Scrubbing HERE fixes the class; editing the committed JSON fixes one
    instance and leaves the next regeneration to reintroduce it (ADR-0077).

    The replacements have to agree with what the audit reports, so the matching is the
    audit's own: `_rule_accepts` is called with the text PRECEDING the match, because
    the home-path rules use it to tell a URL route from a home directory.

    Idempotent by construction -- see `_SCRUB_PLACEHOLDERS` -- which is the property
    that stops a regeneration from silently undoing the scrub.
    """
    for rule, pattern in PATTERNS:
        rewritten: list[str] = []
        cursor = 0
        for match in pattern.finditer(text):
            if not _rule_accepts(rule, match, text[: match.start()]):
                continue
            rewritten += [text[cursor : match.start()], _SCRUB_PLACEHOLDERS[rule]]
            cursor = match.end()
        if rewritten:
            rewritten.append(text[cursor:])
            text = "".join(rewritten)
    return text


# ------------------------------------------------------------------ data


@dataclass
class ProcessUnit:
    host: str
    scheduler: SchedulerKind
    unit: str
    schedule: str
    state: str
    last_exit: str
    command: str
    owned: bool
    area: str
    purpose: str
    naming: str  # "ok" | "violation" | "n/a"

    def to_json(self) -> dict:
        # Every string goes through the scrubber, not just the obvious ones: the unit
        # name, the command and the schedule all come off a live host, and this is the
        # single boundary at which a recorded value becomes the tracked artifact.
        d = {k: scrub_identities(v) if isinstance(v, str) else v for k, v in self.__dict__.items()}
        d["scheduler"] = self.scheduler.value
        return d


@dataclass
class HostResult:
    host: str
    reachable: bool
    error: str | None = None
    units: list[ProcessUnit] = field(default_factory=list)


# --------------------------------------------------------------- probing


def _run(cmd: list[str], timeout: int = 20) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise HostUnreachableError(f"command not found: {cmd[0]}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise HostUnreachableError(f"timed out after {timeout}s: {' '.join(cmd)}") from exc
    if proc.returncode != 0 and "HOST_OK" not in proc.stdout:
        raise HostUnreachableError(
            f"exit {proc.returncode} from {' '.join(cmd)}: {proc.stderr.strip()[:400]}"
        )
    return proc.stdout


def _ssh(alias: str, remote_cmd: str, timeout: int = 25) -> str:
    # HOST_OK sentinel lets us tell "reached the box, remote command itself
    # failed" apart from "never reached the box" -- ssh's own exit code
    # conflates both, which is exactly the ambiguity a fail-loud check must
    # not paper over.
    out = _run(
        [
            "ssh",
            "-o",
            "ConnectTimeout=8",
            "-o",
            "BatchMode=yes",
            alias,
            f"echo HOST_OK; {remote_cmd}",
        ],
        timeout=timeout,
    )
    if "HOST_OK" not in out:
        raise HostUnreachableError(f"ssh to {alias} produced no HOST_OK sentinel")
    return out


__all__ = [
    "GITHUB_ACTIONS_HOST",
    "HOSTS",
    "OWNERSHIP_RULES",
    "Host",
    "HostResult",
    "HostUnreachableError",
    "OwnershipRule",
    "ProcessUnit",
    "SchedulerKind",
    "_run",
    "_ssh",
    "classify",
    "naming_status",
    "scrub_identities",
]
