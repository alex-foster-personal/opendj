"""One CloudSync sync at a time per data dir, in-process AND across processes.

``client.run_sync`` is not re-entrant against one ``state.db``. Three callers
can start it: the background scheduler
(:meth:`apps.sync_hub.scheduler.CloudSyncScheduler.run_round`), the
operator's Sync now (``POST /api/v1/cloudsync/sync``), both inside the engine
daemon process, and ``python -m apps.sync_hub sync`` (:mod:`apps.sync_hub.
maintenance`), which is its OWN process. ``sync_lock_for`` below is a
``threading.Lock`` -- it only ever protected the first two, which share one
process; it cannot see the CLI's process at all. That gap let ``sync --force``
start a second concurrent round against the SAME ``state.db`` while the
engine's scheduler or "Sync now" was mid-round, silently contradicting
CLOUDSYNC-14's own acceptance line that the CLI shares single-flight
protection (claude-review / Codex, PR #3831, P1/BLOCKING).

``sync_flock_for`` closes that gap with an OS file lock (``fcntl.flock`` on
POSIX, ``msvcrt.locking`` on Windows) on a lock file in ``data_dir`` -- the
same non-blocking-exclusive-lock-on-a-data-dir-file pattern
:class:`apps.engine_core.lock.EngineLock` already uses for the engine
singleton itself, which works across processes because the kernel, not this
module's memory, is the registry. :func:`apps.sync_hub.maintenance.sync` is
the ONE function all three callers funnel through to reach ``client.run_sync``
(the scheduler and the HTTP route journal through it too), so wrapping that
call there with ``sync_flock_for`` protects all three without needing the
scheduler or the HTTP route to change. The pre-existing ``sync_lock_for``
in-process lock stays in place unchanged, ahead of it, as a cheap first
check with a friendlier 409 for HTTP callers; the flock is the authoritative
layer underneath that also reaches the CLI.

Keyed by the RESOLVED data dir, so two spellings of one directory share a
lock and two different data dirs (the test hub and spoke in one process)
never block each other.
"""

from __future__ import annotations

import contextlib
import errno
import functools
import os
import sys
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

_GUARD = threading.Lock()
_LOCKS: dict[Path, threading.Lock] = {}

#: Advisory cross-process lock file, one per data dir, sitting beside the
#: engine's own ``.engine.lock`` (``apps.engine_core.config.EngineConfig.
#: lock_path``) rather than inside ``state/`` -- it guards a sync ROUND, not
#: the database file itself.
SYNC_LOCK_FILENAME = ".sync.lock"


class SyncInProgressError(RuntimeError):
    """Another process already holds this data dir's cross-process sync lock."""


def sync_lock_for(data_dir: Path) -> threading.Lock:
    """The one in-process lock every sync against ``data_dir`` must hold.
    Callers acquire it non-blocking and refuse (busy / 409) when it is taken.
    Only ever visible within ONE process -- see :func:`sync_flock_for` for
    the cross-process layer underneath this."""
    key = Path(data_dir).resolve()
    with _GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = _LOCKS[key] = threading.Lock()
        return lock


# ----- platform lock primitives: one non-blocking exclusive lock per fd -----


def _try_lock_posix(fd: int) -> bool:
    """``fcntl.flock`` exclusive, non-blocking. False means another holder."""
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno not in (errno.EACCES, errno.EAGAIN):
            raise
        return False
    return True


def _unlock_posix(fd: int) -> None:
    fcntl.flock(fd, fcntl.LOCK_UN)


@dataclass(frozen=True)
class _MsvcrtLocking:
    """The three ``msvcrt`` names the Windows lock uses, passed in rather than
    imported here so the logic type-checks and tests on any host."""

    locking: Callable[[int, int, int], None]
    lk_nblck: int
    lk_unlck: int


def _try_lock_nt(fd: int, api: _MsvcrtLocking) -> bool:
    """``msvcrt.locking`` on byte 0, non-blocking (``LK_NBLCK``), the Windows
    counterpart of :func:`_try_lock_posix` (the same primitive
    :func:`apps.webui.server.dedup_decisions.decision_file_lock` uses). The
    CRT reports a byte another handle holds as EACCES. ``msvcrt.locking``
    starts at the CURRENT file position, so it seeks to 0 first; Windows
    allows locking past end of file, so the empty lock file needs no byte."""
    os.lseek(fd, 0, os.SEEK_SET)
    try:
        api.locking(fd, api.lk_nblck, 1)
    except OSError as exc:
        if exc.errno != errno.EACCES:
            raise
        return False
    return True


def _unlock_nt(fd: int, api: _MsvcrtLocking) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    api.locking(fd, api.lk_unlck, 1)


_try_lock: Callable[[int], bool]
_unlock: Callable[[int], None]
if sys.platform == "win32":
    import msvcrt

    _MSVCRT = _MsvcrtLocking(msvcrt.locking, msvcrt.LK_NBLCK, msvcrt.LK_UNLCK)
    _try_lock = functools.partial(_try_lock_nt, api=_MSVCRT)
    _unlock = functools.partial(_unlock_nt, api=_MSVCRT)
elif os.name == "posix":
    import fcntl

    _try_lock, _unlock = _try_lock_posix, _unlock_posix
else:
    raise ImportError(f"single_flight has no cross-process lock for {sys.platform=}")


@contextlib.contextmanager
def sync_flock_for(data_dir: Path) -> Iterator[None]:
    """Hold ``data_dir``'s cross-process sync lock for one round, or raise
    :class:`SyncInProgressError` immediately (never blocks) when another
    process already holds it.

    ``fcntl.flock`` on POSIX, ``msvcrt.locking`` on Windows: the ``python -m
    apps.sync_hub sync`` CLI imports and runs on Windows, so this lock must
    not be the thing that stops it there (Sol, PR #3831, P1/BLOCKING).
    """
    resolved = Path(data_dir).resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    lock_path = resolved / SYNC_LOCK_FILENAME
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if not _try_lock(fd):
            raise SyncInProgressError(
                f"a CloudSync sync round is already running against "
                f"{resolved} (held by another process via {lock_path})"
            )
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


__all__ = [
    "SYNC_LOCK_FILENAME",
    "SyncInProgressError",
    "sync_flock_for",
    "sync_lock_for",
]
