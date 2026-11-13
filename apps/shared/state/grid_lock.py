"""The user's beatgrid lock for one track (GRIDEDIT-9763).

rekordbox lets a DJ lock a track's grid; Open DJ mirrors it. ``PUT
/api/v1/tracks/{id}/grid-lock`` stores the lock as the provenance-wrapped
``track_fields`` row ``grid_lock`` (source ``webui``), beside the
``beatgrid_edit`` row, so it survives restart and keeps history. While it is
set, every grid edit route answers 409 ``GRID_LOCKED`` and the canonical own
beatgrid pointer stays on the record it named when the lock was set, so a
re-analysis is recorded but never replaces the grid the deck draws.
Unlocking writes ``{"v": 1, "locked": false}``.

This module owns no connection and opens no transaction.

-Claude
"""

from __future__ import annotations

import json
import sqlite3
from typing import Final

FIELD: Final = "grid_lock"
BLOB_VERSION: Final = 1
OWN_SOURCE: Final = "webui"
#: Error code every refused grid write carries.
LOCKED_CODE: Final = "GRID_LOCKED"


def blob(locked: bool) -> dict[str, object]:
    """The stored value for a lock state."""
    return {"v": BLOB_VERSION, "locked": locked}


def read(conn: sqlite3.Connection, stable_id: str) -> bool:
    """True while the track's grid is locked; False when unset, cleared, or the table is absent.

    Raises ValueError for a stored value this version cannot read, rather than
    guessing whether the user meant the grid to be protected.
    """
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='track_fields'").fetchone():
        return False
    row = conn.execute(
        "SELECT value_json FROM track_fields WHERE stable_id = ? AND field_name = ? AND deleted_at IS NULL",
        (stable_id, FIELD),
    ).fetchone()
    if row is None:
        return False
    value = json.loads(str(row[0]))
    if not (isinstance(value, dict) and value.get("v") == BLOB_VERSION and isinstance(value.get("locked"), bool)):
        raise ValueError(f"{FIELD} for {stable_id} is not a v{BLOB_VERSION} lock: {row[0]!r}")
    return bool(value["locked"])


def locked_message(stable_id: str) -> str:
    """The reason a refused grid write gives."""
    return f"the beatgrid of {stable_id} is locked; unlock it to edit the grid"


__all__ = ["BLOB_VERSION", "FIELD", "LOCKED_CODE", "OWN_SOURCE", "blob", "locked_message", "read"]
