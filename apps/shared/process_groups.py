"""POSIX process-group liveness, signalling, and the wait that confirms death.

Two subsystems terminate a worker tree they spawned with
``start_new_session=True``: ``apps/vocals/cli.py::_terminate_worker_tree`` and
``apps/engine_core/jobs/reap.py``. Both need the same answer to the same
deceptively simple question -- "is anything still running in process group N?"
-- and both got it wrong in the same way before this module existed.

THE TRAP THIS MODULE EXISTS TO CLOSE. ``killpg(pgid, 0)`` is the obvious
liveness probe and it is not one. A group whose every member is a ZOMBIE (dead,
but not yet waited on by its parent) keeps its pgid allocated, and on macOS it
answers that probe with EPERM rather than ESRCH. Read EPERM as "exists" and a
caller concludes the group survived, burns its entire grace budget polling a
corpse, and then reports a live worker that has in fact been dead the whole
time. Read it as "gone" and a group you genuinely may not signal looks reaped.
Neither reading is safe, because the syscall cannot distinguish the two cases.

So the probe is split in two. ``process_group_exists`` keeps the cheap syscall
and keeps its narrow meaning: the pgid is still ALLOCATED. ``group_has_live
_member`` answers the question callers actually have -- is anything in there
still EXECUTING -- by walking the process table and discounting zombies. The
walk costs an enumeration of every process on the box, which is why
``wait_group_gone`` runs the two on different cadences rather than picking one.

A member whose status cannot be read counts as LIVE throughout. Failing to
inspect something is not evidence that it is dead, and the asymmetry is
deliberate: over-reporting liveness costs a slower reap, under-reporting it
costs a leaked worker tree that every caller believes was collected.
"""

from __future__ import annotations

import os
import time

import psutil

# Polling cadence for the cheap "is the pgid still allocated" syscall.
_POLL_S: float = 0.02

# The live-member walk enumerates every process on the box, so it runs on a
# far slower beat than the one-syscall group check it backs up.
_LIVE_CHECK_EVERY_S: float = 0.25


class WorkerCleanupError(RuntimeError):
    """The process group could not be confirmed dead.

    Shared by both callers on purpose: one failure mode deserves one exception
    type, so a caller that catches it does not have to know which subsystem
    spawned the tree. ``apps/vocals/cli.py`` re-exports it under its own name
    because its claim-retention path keys off exactly this error.
    """


def process_group_exists(pgid: int) -> bool:
    """Is the pgid still ALLOCATED? One syscall, and a narrow meaning.

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


def live_group_members(pgid: int) -> tuple[psutil.Process, ...]:
    """Group members that are not zombies.

    A zombie is a dead process whose parent has not waited on it yet. It keeps
    the pgid allocated and, on macOS, makes ``killpg`` answer EPERM -- which is
    how an uncaught PermissionError used to escape a reap. Nothing of it is
    still executing, so for cleanup purposes it does not count. A member whose
    status cannot be read IS counted: failing to inspect something is not
    evidence that it is dead.

    TWO GATES EXCLUDE A ZOMBIE, and which one fires is platform-dependent.
    Measured on darwin: ``psutil.process_iter`` DOES yield the corpse and
    ``psutil.Process(pid).status()`` DOES report "zombie", but ``os.getpgid``
    on it raises ProcessLookupError -- so ``group_members`` has already
    dropped it and the status check below never sees it. The status check is
    the gate that fires where getpgid still answers for a corpse. Do not
    "simplify" either one on the evidence of a single platform: the
    ProcessLookupError arm of ``group_members`` reads like defensive noise and
    is load-bearing here.
    """
    live: list[psutil.Process] = []
    for proc in group_members(pgid):
        try:
            if proc.status() == psutil.STATUS_ZOMBIE:
                continue
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            pass
        live.append(proc)
    return tuple(live)


def group_has_live_member(pgid: int) -> bool:
    """Is anything in the group still RUNNING (as opposed to merely present)?"""
    return bool(live_group_members(pgid))


def signal_group(pgid: int, sig: int) -> str | None:
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
            "live members this process does not own"
        ) from exc
    return None


def wait_group_gone(pgid: int, timeout_s: float) -> bool:
    """True once no LIVE process remains in the group.

    Two cadences on purpose: ``process_group_exists`` is one syscall and runs
    every poll, while the live-member walk enumerates every process on the box
    and runs on a much slower beat. Without the walk a zombie group never looks
    gone, so a ladder burned its full grace and then took EPERM on SIGKILL.
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
    "WorkerCleanupError",
    "group_has_live_member",
    "group_members",
    "live_group_members",
    "process_group_exists",
    "signal_group",
    "wait_group_gone",
]
