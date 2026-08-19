"""Identity-verified process-group termination.

The SIGTERM -> grace -> SIGKILL -> confirm-gone ladder is adapted from
``apps/vocals/cli.py::_terminate_worker_tree`` (credit: that module already
proved the pattern for the vocal worker tree). What is added here is
IDENTITY VERIFICATION.

A pgid recorded in the jobs db outlives the engine that wrote it. After a
crash the OS is free to hand that number to an unrelated process, so a blind
``killpg`` on a stored id can kill a stranger. Every kill in this module is
gated on three checks first: the pid exists, its create_time matches what we
recorded at spawn, and its argv matches what we spawned. Anything short of
all three is a REFUSAL to kill, reported as an outcome string rather than a
silent skip.
"""

from __future__ import annotations

import os
import signal
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass

import psutil

WORKER_TERMINATE_GRACE_S: float = 10.0

# psutil create_time and the wall clock we fall back to at spawn are both
# taken within milliseconds of the fork, so 2s is a generous identity window
# that still fails a pid recycled minutes later.
CREATE_TIME_TOLERANCE_S: float = 2.0

_POLL_S: float = 0.02

# The live-member walk enumerates every process on the box, so it runs on a
# far slower beat than the one-syscall group check it backs up.
_LIVE_CHECK_EVERY_S: float = 0.25


class WorkerCleanupError(RuntimeError):
    """The process group could not be confirmed dead."""


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


def process_group_exists(pgid: int) -> bool:
    """Adapted from apps/vocals/cli.py::_posix_process_group_exists.

    Note what this does NOT mean: a group whose every member is a zombie still
    answers yes, because the pgid stays allocated until the parent waits. Use
    ``group_has_live_member`` for "is anything still running in there".
    """
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def group_members(pgid: int) -> tuple[psutil.Process, ...]:
    """Every live process whose process group is ``pgid``, leader included.

    psutil has no portable pgid attribute, so the group is resolved with
    ``os.getpgid`` per pid. Processes that exit mid-walk are skipped rather
    than raising: the walk is a snapshot of a moving target by definition.
    """
    found: list[psutil.Process] = []
    for proc in psutil.process_iter():
        try:
            if os.getpgid(proc.pid) == pgid:
                found.append(proc)
        except (ProcessLookupError, PermissionError, OSError, psutil.Error):
            continue
    return tuple(found)


def group_has_live_member(pgid: int) -> bool:
    """Is anything in the group still RUNNING (as opposed to merely present)?

    A zombie is a dead process whose parent has not waited on it yet. It keeps
    the pgid allocated and, on macOS, makes ``killpg`` answer EPERM -- which is
    how an uncaught PermissionError used to escape a reap. Nothing of it is
    still executing, so for cleanup purposes the group is dead.
    """
    for proc in group_members(pgid):
        try:
            if proc.status() != psutil.STATUS_ZOMBIE:
                return True
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            return True  # not inspectable, so it must be assumed alive
    return False


def identity_mismatch(identity: WorkerIdentity) -> str | None:
    """Return why the live pid is NOT our worker, or None when it is ours."""
    try:
        proc = psutil.Process(identity.pgid)
    except psutil.NoSuchProcess:
        return f"pid {identity.pgid} is not running"
    try:
        create_time = proc.create_time()
        cmdline = tuple(proc.cmdline())
    except psutil.NoSuchProcess:
        return f"pid {identity.pgid} exited during verification"
    except psutil.AccessDenied:
        return f"pid {identity.pgid} is not inspectable (access denied)"
    drift = abs(create_time - identity.started_at)
    if drift > CREATE_TIME_TOLERANCE_S:
        return (
            f"pid {identity.pgid} create_time drifted {drift:.1f}s from the "
            "recorded spawn; the pid was recycled"
        )
    if cmdline != identity.argv:
        return (
            f"pid {identity.pgid} argv {list(cmdline)!r} does not match the "
            f"recorded {list(identity.argv)!r}"
        )
    try:
        if os.getpgid(identity.pgid) != identity.pgid:
            return f"pid {identity.pgid} is no longer its own group leader"
    except ProcessLookupError:
        return f"pid {identity.pgid} exited during verification"
    return None


def terminate_group(
    pgid: int, *, grace_s: float = WORKER_TERMINATE_GRACE_S
) -> str:
    """SIGTERM -> grace -> SIGKILL -> confirm gone. Returns the outcome.

    Adapted from apps/vocals/cli.py::_terminate_worker_tree. Raises
    WorkerCleanupError when the group survives SIGKILL, because an
    unverifiable kill is worse than a loud one.
    """
    _require_posix()
    already = _signal_group(pgid, signal.SIGTERM)
    if already is not None:
        return already
    if _wait_gone(pgid, grace_s):
        return f"process group {pgid} exited on SIGTERM"
    already = _signal_group(pgid, signal.SIGKILL)
    if already is not None:
        return already
    if not _wait_gone(pgid, grace_s):
        raise WorkerCleanupError(
            f"process group {pgid} survived SIGKILL after {grace_s:.0f}s"
        )
    return f"process group {pgid} killed after a {grace_s:.0f}s SIGTERM grace"


def _signal_group(pgid: int, sig: int) -> str | None:
    """Send ``sig``, or return the sentence explaining why there was no need.

    PermissionError is not an error path when the group holds only zombies:
    macOS refuses signals to a group with no live member, which is the very
    condition that means the work is already done. A group that DOES have live
    members and still refuses the signal is somebody else's, and that is a
    loud failure rather than a silent skip.
    """
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return f"process group {pgid} was already gone"
    except PermissionError as exc:
        if not group_has_live_member(pgid):
            return (
                f"process group {pgid} holds only dead (zombie) members; "
                "there was nothing left to kill"
            )
        raise WorkerCleanupError(
            f"not permitted to signal process group {pgid} ({exc}); it has "
            "live members this engine does not own"
        ) from exc
    return None


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
    """Verify then kill, reporting whether the group is provably clear."""
    mismatch = identity_mismatch(identity)
    if mismatch is not None:
        # Verification PROVED the pgid is not our worker, so there is nothing
        # of ours left to kill and retrying on every future boot would just
        # re-refuse forever.
        return ReapResult(f"reap skipped: {mismatch}", group_cleared=True)
    try:
        outcome = terminate_group(identity.pgid)
    except WorkerCleanupError as exc:
        return ReapResult(f"reap FAILED: {exc}", group_cleared=False)
    return ReapResult(
        f"reap: {outcome}",
        group_cleared=not group_has_live_member(identity.pgid),
    )


def reap(identity: WorkerIdentity) -> str:
    """Verify then kill. Returns a sentence for the jobs row's error column."""
    return reap_group(identity).outcome


def _wait_gone(pgid: int, timeout_s: float) -> bool:
    """True once no LIVE process remains in the group.

    Two cadences on purpose: ``process_group_exists`` is one syscall and runs
    every poll, while the live-member walk enumerates every process on the box
    and runs on a much slower beat. Without the walk a zombie group never looks
    gone, so the ladder burned the full grace and then took EPERM on SIGKILL.
    """
    deadline = time.monotonic() + timeout_s
    next_live_check = 0.0
    while process_group_exists(pgid):
        now = time.monotonic()
        if now >= next_live_check:
            if not group_has_live_member(pgid):
                return True
            next_live_check = now + _LIVE_CHECK_EVERY_S
        if now >= deadline:
            return not group_has_live_member(pgid)
        time.sleep(_POLL_S)
    return True


__all__ = [
    "CREATE_TIME_TOLERANCE_S",
    "WORKER_TERMINATE_GRACE_S",
    "ReapResult",
    "WorkerCleanupError",
    "WorkerIdentity",
    "group_has_live_member",
    "group_members",
    "identity_mismatch",
    "process_group_exists",
    "reap",
    "reap_group",
    "terminate_group",
]
