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

``sync_flock_for`` closes that gap with an ``fcntl.flock`` on a lock file in
``data_dir`` -- the same non-blocking-exclusive-flock-on-a-data-dir-file
pattern :class:`apps.engine_core.lock.EngineLock` already uses for the engine
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
import os
import threading
from collections.abc import Iterator
from pathlib import Path

if os.name == "posix":
    import fcntl

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


@contextlib.contextmanager
def sync_flock_for(data_dir: Path) -> Iterator[None]:
    """Hold ``data_dir``'s cross-process sync flock for one round, or raise
    :class:`SyncInProgressError` immediately (never blocks) when another
    process already holds it.

    POSIX-only (``fcntl.flock``, same as ``EngineLock``); this repo's CI and
    shipped desktop targets are both POSIX, and a platform without ``fcntl``
    fails loudly here rather than silently skipping the lock.
    """
    if os.name != "posix":
        raise NotImplementedError(
            "sync_flock_for requires fcntl.flock, which is POSIX-only; "
            f"unsupported on os.name={os.name!r}"
        )
    resolved = Path(data_dir).resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    lock_path = resolved / SYNC_LOCK_FILENAME
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            raise SyncInProgressError(
                f"a CloudSync sync round is already running against "
                f"{resolved} (held by another process via {lock_path})"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


__all__ = [
    "SYNC_LOCK_FILENAME",
    "SyncInProgressError",
    "sync_flock_for",
    "sync_lock_for",
]
