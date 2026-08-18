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
    """Adapted from apps/vocals/cli.py::_posix_process_group_exists."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


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
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return f"process group {pgid} was already gone"
    if _wait_gone(pgid, grace_s):
        return f"process group {pgid} exited on SIGTERM"
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return f"process group {pgid} exited during the SIGTERM grace"
    if not _wait_gone(pgid, grace_s):
        raise WorkerCleanupError(
            f"process group {pgid} survived SIGKILL after {grace_s:.0f}s"
        )
    return f"process group {pgid} killed after a {grace_s:.0f}s SIGTERM grace"


def reap(identity: WorkerIdentity) -> str:
    """Verify then kill. Returns a sentence for the jobs row's error column."""
    mismatch = identity_mismatch(identity)
    if mismatch is not None:
        return f"reap skipped: {mismatch}"
    try:
        return f"reap: {terminate_group(identity.pgid)}"
    except WorkerCleanupError as exc:
        return f"reap FAILED: {exc}"


def _wait_gone(pgid: int, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while process_group_exists(pgid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL_S)
    return True


__all__ = [
    "CREATE_TIME_TOLERANCE_S",
    "WORKER_TERMINATE_GRACE_S",
    "WorkerCleanupError",
    "WorkerIdentity",
    "identity_mismatch",
    "process_group_exists",
    "reap",
    "terminate_group",
]
