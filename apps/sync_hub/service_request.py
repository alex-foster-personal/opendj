"""Request wiring every hub router uses to find the state DB and data dir.

Split out of :mod:`apps.sync_hub.service` (and the two asset routers split
from it) so ``_db_path`` / ``_data_dir`` have one body. The hub opens the
DB per request from ``app.state.state_db_path``; a missing path is 503
``SYNC_NO_DB``, not a guessed fallback.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fastapi import HTTPException, Request

from apps.shared.state import db as state_db

# Preserved from pre-#5304 routers: main sync vs asset sub-routers used different text.
SYNC_NO_DB_MESSAGE_MAIN = (
    "app.state.state_db_path is unset; the hub has no state DB to sync against."
)
SYNC_NO_DB_MESSAGE_ASSET_ROUTERS = "app.state.state_db_path is unset"


def _db_path(request: Request, *, sync_no_db_message: str = SYNC_NO_DB_MESSAGE_MAIN) -> Path:
    configured = getattr(request.app.state, "state_db_path", None)
    if configured is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "SYNC_NO_DB",
                "message": sync_no_db_message,
            },
        )
    return Path(str(configured))


def _data_dir(request: Request) -> Path:
    """Where this hub's ``machine-id`` file lives.

    Derived from the DB path (``<data-dir>/state/state.db``) unless the app
    sets ``sync_hub_data_dir`` explicitly. No env fallback: a hub that cannot
    say which data root it owns should fail, not adopt a plausible one.
    """
    configured = getattr(request.app.state, "sync_hub_data_dir", None)
    if configured is not None:
        return Path(str(configured))
    return _db_path(request).resolve().parent.parent


@contextmanager
def _hub_conn(request: Request) -> Iterator[sqlite3.Connection]:
    conn = state_db.open_rw(_db_path(request))
    try:
        yield conn
    finally:
        conn.close()
