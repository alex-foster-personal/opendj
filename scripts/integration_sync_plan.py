"""Pure planning for the integration sync: refs, actions and the per-trunk decision.

Split from ``scripts/integration_sync.py`` (#10976). Import the names from
``scripts.integration_sync``; this module is an implementation slice with no IO.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

SYNC_PREFIX = "sync"
# Hours a sync commit may sit with no run of any required check before the job
# gives up on its CI (dispatch lost, run cancelled before the gate, no runner).
STUCK_HOURS = 6


class PreconditionError(RuntimeError):
    """The job could not measure; it must report UNKNOWN, never a verdict."""


@dataclass(frozen=True)
class SyncRef:
    """One bot-owned `sync--<trunk>--<sha12>` branch and what is known about its head."""

    name: str
    sha: str
    # The head is a sync merge of the trunk tip (FIRST parent) and the upstream tip its name
    # carries (SECOND parent), so a fast-forward to it keeps the trunk's first-parent history
    # and lands only the upstream this branch was made for. False once the trunk moved after
    # the merge, or when the head is not that merge (a stale, corrupted or hand-made branch).
    on_trunk_tip: bool
    # Required check name -> conclusion ("success", "failure", ...) or None while
    # the run is queued or in progress. A required check with NO run is absent
    # from the mapping, which reads as pending, never as passed.
    checks: dict[str, str | None] = field(default_factory=dict)
    # Committer date of the head commit, for the stuck-CI cap.
    committed_at: datetime | None = None


@dataclass(frozen=True)
class Action:
    kind: str  # up-to-date | create | advance | delete | refresh | wait | stuck | red | idle
    ref: str = ""
    sha: str = ""
    note: str = ""


def sync_ref_name(trunk: str, upstream_sha: str) -> str:
    # `--` separators, not `/`: the repo is migrating its branch names off slashes.
    return f"{SYNC_PREFIX}--{trunk}--{upstream_sha[:12]}"


def conflict_ref_name(trunk: str, upstream_sha: str) -> str:
    return f"{SYNC_PREFIX}--{trunk}--conflict--{upstream_sha[:12]}"


def _is_sync_ref(name: str, trunk: str) -> bool:
    """`sync--<trunk>--<sha12>` exactly; a conflict branch or a longer trunk name is not ours."""
    prefix = f"{SYNC_PREFIX}--{trunk}--"
    rest = name.removeprefix(prefix)
    return name.startswith(prefix) and len(rest) == 12 and all(c in "0123456789abcdef" for c in rest)


def is_green(ref: SyncRef, required: list[str]) -> bool:
    """Presence of success for every required check, not absence of failure."""
    if not required:
        raise PreconditionError("no required checks configured; a sync would advance untested")
    return all(ref.checks.get(name) == "success" for name in required)


def is_red(ref: SyncRef, required: list[str]) -> bool:
    return any(
        name in ref.checks and ref.checks[name] is not None and ref.checks[name] != "success" for name in required
    )


def red_note(ref: SyncRef, required: list[str]) -> str:
    """Names each required check that concluded non-success, so an operator can see which gate to repair."""
    failed = [f"{name} {ref.checks[name]}" for name in required if is_red(ref, [name])]
    return "required check failed at this upstream tip: " + ", ".join(failed)


def is_stuck(ref: SyncRef, required: list[str], now: datetime) -> bool:
    """No required check has any run, long after the sync commit was made."""
    if any(name in ref.checks for name in required) or ref.committed_at is None:
        return False
    return (now - ref.committed_at).total_seconds() > STUCK_HOURS * 3600


def plan_trunk(
    *,
    trunk: str,
    trunk_sha: str,
    upstream_sha: str,
    behind_by: int,
    sync_refs: list[SyncRef],
    required: list[str],
    auto_ff: bool,
    advance_only: bool = False,
) -> list[Action]:
    """Pure decision for one trunk. IO happens in run_trunk."""
    current = sync_ref_name(trunk, upstream_sha)
    actions, live = _plan_moved_trunk(sync_refs, current, trunk_sha)
    if actions and actions[-1].kind == "refresh":
        if behind_by > 0:
            return actions
        # The trunk already holds this upstream tip (R1: a no-op), so the pending sync is not needed.
        # Drop it rather than rebuild it and dispatch a redundant CI run.
        stale = actions.pop()
        actions.append(Action("delete", stale.ref, stale.sha, "trunk already contains the upstream tip"))
        actions.append(Action("up-to-date", note=f"{trunk} contains {upstream_sha[:12]}"))
        return actions
    done = _plan_live(live, required, auto_ff, current, actions)
    if done:
        return actions
    if behind_by == 0:
        actions.append(Action("up-to-date", note=f"{trunk} contains {upstream_sha[:12]}"))
    elif advance_only:
        actions.append(Action("idle", note=f"{behind_by} upstream commits wait for the daily pass"))
    else:
        actions.append(Action("create", current, upstream_sha, f"{behind_by} upstream commits to merge"))
    return actions


def _plan_moved_trunk(sync_refs: list[SyncRef], current: str, trunk_sha: str) -> tuple[list[Action], list[SyncRef]]:
    """Split sync branches into live ones and the ones the trunk moved past."""
    actions: list[Action] = []
    live: list[SyncRef] = []
    for ref in sync_refs:
        if ref.on_trunk_tip:
            live.append(ref)
        elif ref.name == current:
            # Still the sync this trunk needs: rebuild it on the new trunk tip in this
            # same pass, so a busy trunk cannot starve it. Rebuilt, not merged into:
            # merging the trunk into the sync branch would make the OLD sync head the
            # first parent and bend the trunk's first-parent history on advance.
            actions.append(Action("refresh", ref.name, ref.sha, f"trunk moved to {trunk_sha[:12]}; rebuilding on it"))
            return actions, []
        else:
            actions.append(Action("delete", ref.name, ref.sha, "trunk moved past it, and upstream has moved on"))
    return actions, live


def _plan_live(live: list[SyncRef], required: list[str], auto_ff: bool, current: str, actions: list[Action]) -> bool:
    """Append what to do with the live sync branches; True when nothing new may start."""
    for ref in live:
        if is_green(ref, required):
            if auto_ff:
                actions.append(Action("advance", ref.name, ref.sha, "required checks green"))
                # The trunk moves this pass; the next pass plans from its new tip.
                actions.append(Action("delete", ref.name, ref.sha, "advanced"))
            else:
                actions.append(Action("wait", ref.name, ref.sha, "green; auto fast-forward is off"))
            return True

    now = datetime.now(UTC)
    pending = [r for r in live if not is_red(r, required)]
    stuck = [r for r in pending if is_stuck(r, required, now)]
    note = f"no {' / '.join(required)} run {STUCK_HOURS}h after the sync commit; deleted so the next pass retries"
    actions.extend(Action("stuck", r.name, r.sha, note) for r in stuck)
    pending = [r for r in pending if r not in stuck]
    if pending:
        actions.extend(Action("wait", r.name, r.sha, "required checks not finished") for r in pending)
        return True
    # A stuck branch is deleted above and not rebuilt in this pass's plan, so a fresh sync may start in the same
    # pass rather than waiting for the next daily run.
    for ref in (r for r in live if is_red(r, required)):
        if ref.name == current:
            actions.append(Action("red", ref.name, ref.sha, red_note(ref, required)))
            return True
        actions.append(Action("delete", ref.name, ref.sha, "red, superseded by a newer upstream tip"))
    return False
