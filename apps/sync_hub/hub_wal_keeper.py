"""Keep a hub database's WAL open between requests (LIBM-120 L6).

Every hub request opens its own connection and closes it when it answers. On
a WAL database the LAST connection to close checkpoints the whole WAL into
the database, syncs it and deletes the WAL file; the next request's first
commit then syncs a brand-new WAL. A 10,000-track first sync makes about 150
hub requests, and the real hub process spent 2.9 s in ``close()`` and 2.9 s in
``COMMIT`` doing exactly that (py-spy, agentbox, Tue 29 Sep 2026).

One idle connection per database, held for the life of the app, means a
request's ``close()`` is never the last one: the WAL persists, and SQLite's own
auto-checkpoint (every 1,000 pages, at commit) keeps it bounded. The keeper
runs no transaction after its first read, so it never pins the WAL and never
blocks a checkpoint. This is the usual shape of a long-lived SQLite server:
the process holds the database open, and requests borrow connections.

The keeper is not closed explicitly. It is dropped with the app, and a process
that exits with it open leaves a WAL the next open recovers, as after any
crash.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

# ----- config -------------------------------------------------------------------


class CFG:
    #: ``app.state`` attribute holding ``{resolved db path: keeper connection}``.
    STATE_ATTR: str = "sync_hub_wal_keepers"


_LOCK = threading.Lock()


# ----- keeper -------------------------------------------------------------------


def keep_wal_open(app_state: Any, db_path: Path) -> None:
    """Hold one idle connection to ``db_path`` for as long as ``app_state`` lives.

    Idempotent per resolved path. Call it after the request's own connection
    has opened (and migrated) the database, so the keeper attaches to a WAL
    database that exists.
    """
    key = str(Path(db_path).resolve())
    with _LOCK:
        keepers: dict[str, sqlite3.Connection] | None = getattr(app_state, CFG.STATE_ATTR, None)
        if keepers is None:
            keepers = {}
            setattr(app_state, CFG.STATE_ATTR, keepers)
        if key in keepers:
            return
        keeper = sqlite3.connect(key, check_same_thread=False, isolation_level=None)
        # A connection attaches to the WAL on its first read, not on open; an
        # unread keeper would not stop the next close() from checkpointing.
        keeper.execute("SELECT count(*) FROM sqlite_master").fetchone()
        keepers[key] = keeper


def wal_keepers(app_state: Any) -> dict[str, sqlite3.Connection]:
    """The keepers ``app_state`` holds, by resolved database path."""
    return dict(getattr(app_state, CFG.STATE_ATTR, None) or {})


__all__ = ["CFG", "keep_wal_open", "wal_keepers"]
