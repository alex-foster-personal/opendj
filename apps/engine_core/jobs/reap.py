"""Identity-verified process-group termination.

The SIGTERM -> grace -> SIGKILL -> confirm-gone ladder is adapted from
``apps/vocals/cli.py::_terminate_worker_tree`` (credit: that module already
proved the pattern for the vocal worker tree). What is added here is
IDENTITY VERIFICATION.

The group primitives underneath it -- liveness, the zombie-discounting member
walk, the signal that tolerates an all-zombie group, and the two-cadence wait
-- now live in ``apps.shared.process_groups`` and are re-exported below. They
were written here first and the vocal worker kept its own pre-zombie copy, so
the same EPERM-on-a-corpse bug this module fixed went on burning a full grace
over there. One home means one fix.

A pgid recorded in the jobs db outlives the engine that wrote it. After a
crash the OS is free to hand that number to an unrelated process, so a blind
``killpg`` on a stored id can kill a stranger. Every kill in this module is
gated on three checks first: the pid exists, its create_time matches what we
recorded at spawn, and its argv matches what we spawned. Anything short of
all three is a REFUSAL to kill, reported as an outcome string rather than a
silent skip.

The leader is not the only thing that can leak, though. A worker that spawned
children and then died leaves a group with no leader, where those three checks
can never pass -- and the tree is exactly what a group kill exists to collect.
So a leaderless-but-live group gets a second gate (``_inspect_survivors``)
built from what a CHILD can actually prove: it cannot predate the worker that
forked it. That gate can rule a group out conclusively and rule it in only
circumstantially, and a group it cannot judge at all is left unreaped and said
so, never quietly dropped.
"""

from __future__ import annotations

import os
import signal
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass

import psutil

from apps.shared.process_groups import (
    WorkerCleanupError,
    group_has_live_member,
    group_members,
    live_group_members,
    process_group_exists,
    signal_group,
    wait_group_gone,
)

WORKER_TERMINATE_GRACE_S: float = 10.0

# The share of the grace held back for the SIGKILL rung. A kill is not
# instantaneous either -- the kernel still has to schedule the exit and
# somebody still has to reap it -- so spending the whole budget on SIGTERM
# would leave the confirm no time and turn every stubborn group into a raise
# instead of a kill. Capped at half the grace so a caller passing a small one
# still gets a ladder rather than an immediate SIGKILL.
WORKER_KILL_CONFIRM_S: float = 2.0

# psutil create_time and the wall clock we fall back to at spawn are both
# taken within milliseconds of the fork, so 2s is a generous identity window
# that still fails a pid recycled minutes later.
CREATE_TIME_TOLERANCE_S: float = 2.0


@dataclass(frozen=True)
class WorkerIdentity:
    """Everything persisted at spawn that makes a later kill provable."""

    pgid: int
    argv: tuple[str, ...]
    started_at: float

    @classmethod
    def capture(cls, pid: int, argv: Sequence[str]) -> WorkerIdentity:
        """Snapshot identity immediately after spawn.

        ``start_new_session=True`` makes the child a session+group leader, so
        its pgid equals its pid. create_time comes from psutil when the child
        is still visible; a child that already exited falls back to the wall
        clock, which is the same instant measured on a coarser source and
        stays inside CREATE_TIME_TOLERANCE_S.
        """
        try:
            started_at = psutil.Process(pid).create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            started_at = time.time()
        return cls(pgid=pid, argv=tuple(argv), started_at=started_at)


def _require_posix() -> None:
    if sys.platform == "win32":
        raise WorkerCleanupError(
            "engine worker reaping is POSIX-only; port the taskkill branch "
            "from apps/vocals/cli.py::_terminate_worker_tree before running "
            "the engine on Windows"
        )


@dataclass(frozen=True)
class _LeaderVerdict:
    """Why the group leader is not usable as proof, and whether it is alive.

    ``alive`` is the branch that matters: a leader that is alive but DIFFERENT
    means the pgid belongs to a stranger and nothing may be killed. A leader
    that is gone says nothing about the children it left behind.
    """

    mismatch: str | None
    alive: bool


def _inspect_leader(identity: WorkerIdentity) -> _LeaderVerdict:
    try:
        proc = psutil.Process(identity.pgid)
    except psutil.NoSuchProcess:
        return _LeaderVerdict(f"pid {identity.pgid} is not running", alive=False)
    try:
        create_time = proc.create_time()
        cmdline = tuple(proc.cmdline())
    except psutil.ZombieProcess:
        # Subclass of NoSuchProcess, so it MUST be caught first.
        return _LeaderVerdict(
            f"pid {identity.pgid} is a zombie; its argv can no longer be read",
            alive=False,
        )
    except psutil.NoSuchProcess:
        return _LeaderVerdict(
            f"pid {identity.pgid} exited during verification", alive=False
        )
    except psutil.AccessDenied:
        return _LeaderVerdict(
            f"pid {identity.pgid} is not inspectable (access denied)", alive=True
        )
    return _readable_leader_verdict(identity, create_time, cmdline)


def _readable_leader_verdict(
    identity: WorkerIdentity, create_time: float, cmdline: tuple[str, ...]
) -> _LeaderVerdict:
    """The three-way gate itself, for a leader that could actually be read."""
    drift = abs(create_time - identity.started_at)
    if drift > CREATE_TIME_TOLERANCE_S:
        return _LeaderVerdict(
            f"pid {identity.pgid} create_time drifted {drift:.1f}s from the "
            "recorded spawn; the pid was recycled",
            alive=True,
        )
    if cmdline != identity.argv and not _renamed_worker_matches(identity, cmdline):
        return _LeaderVerdict(
            f"pid {identity.pgid} argv {list(cmdline)!r} does not match the "
            f"recorded {list(identity.argv)!r}",
            alive=True,
        )
    try:
        if os.getpgid(identity.pgid) != identity.pgid:
            return _LeaderVerdict(
                f"pid {identity.pgid} is no longer its own group leader",
                alive=True,
            )
    except ProcessLookupError:
        return _LeaderVerdict(
            f"pid {identity.pgid} exited during verification", alive=False
        )
    return _LeaderVerdict(None, alive=True)


def _renamed_worker_matches(
    identity: WorkerIdentity, cmdline: tuple[str, ...]
) -> bool:
    """Accept our process-title form without weakening the spawn proof.

    ``setproctitle`` makes psutil expose one argv element. The title must keep
    the exact, shell-escaped spawn argv, so recovery still rejects a pid whose
    executable or arguments differ from the worker it recorded.
    """
    from apps.shared.process_identity import process_command

    expected = process_command(
        "worker", invocation_argv=identity.argv
    )
    return cmdline == (expected,)


def identity_mismatch(identity: WorkerIdentity) -> str | None:
    """Return why the live pid is NOT our worker, or None when it is ours."""
    return _inspect_leader(identity).mismatch


@dataclass(frozen=True)
class _GroupVerdict:
    """May the ladder be applied to a group whose leader is gone?"""

    refusal: str | None
    cleared: bool


def _inspect_survivors(identity: WorkerIdentity) -> _GroupVerdict:
    """Gate a leaderless-but-live group on evidence from ANY survivor.

    The leader's three-way gate cannot be reproduced member for member: a
    child's argv and create_time are its own, not the leader's. What IS
    checkable is that a descendant of our worker cannot PREDATE our worker. So
    a survivor created at or after the recorded spawn is consistent with being
    ours, and a group in which every survivor predates it definitively is not.

    That proves NOT-ours conclusively and ours only circumstantially, which is
    the deliberate asymmetry: the cost of not killing is a leaked tree the row
    still names, and the cost of killing wrongly is a stranger's process. A
    group that cannot be enumerated at all is therefore refused as unreaped
    rather than killed on faith -- never silently either way.
    """
    survivors = live_group_members(identity.pgid)
    floor = identity.started_at - CREATE_TIME_TOLERANCE_S
    ours: list[int] = []
    foreign: list[int] = []
    unreadable: list[int] = []
    for proc in survivors:
        try:
            created = proc.create_time()
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            unreadable.append(proc.pid)
            continue
        (ours if created >= floor else foreign).append(proc.pid)
    if ours:
        return _GroupVerdict(None, cleared=False)
    if unreadable:
        return _GroupVerdict(
            f"process group {identity.pgid} is live but its surviving "
            f"member(s) {unreadable} are not inspectable, so nothing ties them "
            "to the recorded worker; refused to kill an unverifiable group, "
            "which is left UNREAPED",
            cleared=False,
        )
    if foreign:
        return _GroupVerdict(
            f"every surviving member of process group {identity.pgid} "
            f"({foreign}) predates the recorded worker spawn; the pgid was "
            "recycled by an unrelated group",
            cleared=True,
        )
    return _GroupVerdict(
        f"process group {identity.pgid} emptied out during verification",
        cleared=True,
    )


def terminate_group(
    pgid: int, *, grace_s: float = WORKER_TERMINATE_GRACE_S
) -> str:
    """SIGTERM -> SIGKILL -> confirm gone, all inside ONE ``grace_s``.

    WORST CASE IS grace_s IN TOTAL, per call. Both rungs draw on the same
    deadline: SIGTERM gets what is left after reserving WORKER_KILL_CONFIRM_S
    (capped at half the grace) for the confirm, and the confirm gets whatever
    remains, including anything SIGTERM did not spend.

    That total is the number callers budget against. Boot recovery reaps its
    rows in a SERIAL loop on the synchronous boot path, so N orphaned rows cost
    up to N x grace_s before the engine finishes starting -- it used to be
    N x 2 x grace_s, because each rung waited a full grace of its own (C17).

    Adapted from apps/vocals/cli.py::_terminate_worker_tree. Raises
    WorkerCleanupError when the group survives SIGKILL, because an
    unverifiable kill is worse than a loud one.
    """
    _require_posix()
    deadline = time.monotonic() + grace_s
    kill_at = deadline - min(WORKER_KILL_CONFIRM_S, grace_s / 2)
    already = signal_group(pgid, signal.SIGTERM)
    if already is not None:
        return already
    if wait_group_gone(pgid, max(kill_at - time.monotonic(), 0.0)):
        return f"process group {pgid} exited on SIGTERM"
    already = signal_group(pgid, signal.SIGKILL)
    if already is not None:
        return already
    if not wait_group_gone(pgid, max(deadline - time.monotonic(), 0.0)):
        raise WorkerCleanupError(
            f"process group {pgid} survived SIGKILL; it was still live "
            f"{grace_s:.0f}s after the SIGTERM that opened the ladder"
        )
    return (
        f"process group {pgid} killed within the {grace_s:.0f}s SIGTERM grace"
    )


@dataclass(frozen=True)
class ReapResult:
    """What a reap attempt achieved, in a form a caller can BRANCH on.

    ``outcome`` is the sentence for the jobs row's error column.
    ``group_cleared`` says whether anything of OURS can still be running in
    that group. Boot recovery uses it to decide whether the row is done with
    or has to be tried again on the next boot, which is a decision that must
    never rest on substring-matching a human-readable sentence.
    """

    outcome: str
    group_cleared: bool


def reap_group(identity: WorkerIdentity) -> ReapResult:
    """Verify then kill, reporting whether the group is provably clear.

    Three routes to a kill decision, in descending order of proof:

      1. the group LEADER passes the three-way identity gate -- kill;
      2. the leader is alive but is somebody else -- the whole pgid belongs to
         a stranger, so refuse;
      3. the leader is gone but the group is still live -- the children it
         orphaned are the thing that leaks, so gate on survivor evidence
         instead of walking away.

    Route 3 is the one that used to be missing entirely: a dead leader made
    identity_mismatch report "pid N is not running", the reap was skipped, and
    the whole surviving tree leaked.
    """
    unusable = _unusable_pgid(identity.pgid)
    if unusable is not None:
        return ReapResult(f"reap skipped: {unusable}", group_cleared=True)
    leader = _inspect_leader(identity)
    if leader.mismatch is None:
        return _apply_ladder(identity.pgid, "leader identity verified")
    if leader.alive:
        # A live pid that is NOT ours owns this pgid, and so does its tree.
        return ReapResult(f"reap skipped: {leader.mismatch}", group_cleared=True)
    if not group_has_live_member(identity.pgid):
        return ReapResult(f"reap skipped: {leader.mismatch}", group_cleared=True)
    verdict = _inspect_survivors(identity)
    if verdict.refusal is not None:
        return ReapResult(
            f"reap skipped: {leader.mismatch}, and the group is still live but "
            f"unproven: {verdict.refusal}",
            group_cleared=verdict.cleared,
        )
    return _apply_ladder(
        identity.pgid,
        f"{leader.mismatch}, but the group outlived it and a survivor is "
        "consistent with the recorded spawn",
    )


def _unusable_pgid(pgid: int) -> str | None:
    """Reject pgids that cannot mean what the caller thinks they mean.

    killpg(0, sig) signals the CALLER'S OWN group, so a zero from a corrupt row
    would have the engine kill itself and every worker it holds. Negative and
    1 are equally not ours to signal, and psutil raises ValueError on them.
    """
    if pgid <= 1:
        return (
            f"recorded pgid {pgid} is not a killable process group (0 would "
            "signal this engine's own group); refused"
        )
    return None


def _apply_ladder(pgid: int, why: str) -> ReapResult:
    try:
        outcome = terminate_group(pgid)
    except WorkerCleanupError as exc:
        return ReapResult(f"reap FAILED ({why}): {exc}", group_cleared=False)
    return ReapResult(
        f"reap: {outcome} ({why})",
        group_cleared=not group_has_live_member(pgid),
    )


def reap(identity: WorkerIdentity) -> str:
    """Verify then kill. Returns a sentence for the jobs row's error column."""
    return reap_group(identity).outcome


__all__ = [
    "CREATE_TIME_TOLERANCE_S",
    "WORKER_KILL_CONFIRM_S",
    "WORKER_TERMINATE_GRACE_S",
    "ReapResult",
    "WorkerCleanupError",
    "WorkerIdentity",
    "group_has_live_member",
    "group_members",
    "identity_mismatch",
    "live_group_members",
    "process_group_exists",
    "reap",
    "reap_group",
    "terminate_group",
]
