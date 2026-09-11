"""One CloudSync sync at a time per data dir, per process.

``client.run_sync`` is not re-entrant against one ``state.db``. Two callers
start it in the same daemon: the background scheduler
(:meth:`apps.sync_hub.scheduler.CloudSyncScheduler.run_round`) and the
operator's Sync now (``POST /api/v1/cloudsync/sync``). Each used to hold a
lock of its own, so a Sync now could overlap a scheduler round on the same
database. Both now take the lock this module hands out for their data dir.

Keyed by the RESOLVED data dir, so two spellings of one directory share a
lock and two different data dirs (the test hub and spoke in one process)
never block each other.
"""

from __future__ import annotations

import threading
from pathlib import Path

_GUARD = threading.Lock()
_LOCKS: dict[Path, threading.Lock] = {}


def sync_lock_for(data_dir: Path) -> threading.Lock:
    """The one lock every sync against ``data_dir`` must hold. Callers
    acquire it non-blocking and refuse (busy / 409) when it is taken."""
    key = Path(data_dir).resolve()
    with _GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = _LOCKS[key] = threading.Lock()
        return lock


__all__ = ["sync_lock_for"]
