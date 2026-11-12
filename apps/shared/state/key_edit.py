"""The user's own key for one track, typed into the library key cell (KEY-10229).

rekordbox stores a corrected key with the track; so do we. The library key
cell, ``PUT /api/v1/tracks/{id}/key-edit`` and ``opendj track key-edit`` store
it as the provenance-wrapped ``track_fields`` row ``key_edit`` (source
``webui``), so it survives restart, keeps history and travels with CloudSync
like every other user field. The route parses any notation ``canonical_key_text``
reads and stores canonical Camelot. The library row, the deck's track
read and ``/anlz`` serve it with ``key_source: "edit"``. Clearing it writes
``{"key": null}`` so history keeps the earlier value.

This module owns no connection and opens no transaction.

-Claude
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Sequence
from typing import Final

FIELD: Final = "key_edit"
BLOB_VERSION: Final = 1
OWN_SOURCE: Final = "webui"
INVALID_CODE: Final = "KEY_EDIT_INVALID"

CAMELOT: Final = re.compile(r"(1[0-2]|[1-9])[AB]")
_SQLITE_VARS: Final = 900


def blob(key: str | None) -> dict[str, object]:
    return {"v": BLOB_VERSION, "key": key}


def _parse(stable_id: str, value_json: object) -> str | None:
    value = json.loads(str(value_json))
    key = value.get("key") if isinstance(value, dict) else None
    if not (isinstance(value, dict) and value.get("v") == BLOB_VERSION) or (
        key is not None and not (isinstance(key, str) and CAMELOT.fullmatch(key))
    ):
        raise ValueError(f"{FIELD} for {stable_id} is not a v{BLOB_VERSION} key edit: {value_json!r}")
    return key


def _table_present(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='track_fields'").fetchone() is not None


def read_row(conn: sqlite3.Connection, stable_id: str) -> tuple[str | None, str | None]:
    """``(key, modified_at)``; key None when there is no row, no table, or it was cleared.

    Raises ValueError for a stored value this version cannot read, rather than
    serving a key the user did not type.
    """
    if not _table_present(conn):
        return None, None
    row = conn.execute(
        "SELECT value_json, modified_at FROM track_fields WHERE stable_id = ? AND field_name = ? AND deleted_at IS NULL",
        (stable_id, FIELD),
    ).fetchone()
    if row is None:
        return None, None
    return _parse(stable_id, row[0]), (str(row[1]) if row[1] is not None else None)


def read(conn: sqlite3.Connection, stable_id: str) -> str | None:
    return read_row(conn, stable_id)[0]


def read_many(conn: sqlite3.Connection, stable_ids: Sequence[str]) -> dict[str, str]:
    """The edits in force for a page of tracks; tracks without one are absent."""
    if not stable_ids or not _table_present(conn):
        return {}
    out: dict[str, str] = {}
    ids = list(stable_ids)
    for start in range(0, len(ids), _SQLITE_VARS):
        chunk = ids[start : start + _SQLITE_VARS]
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT stable_id, value_json FROM track_fields WHERE field_name = ? AND deleted_at IS NULL AND stable_id IN ({marks})",
            (FIELD, *chunk),
        ).fetchall()
        for sid, value_json in rows:
            key = _parse(str(sid), value_json)
            if key is not None:
                out[str(sid)] = key
    return out


__all__ = [
    "BLOB_VERSION",
    "CAMELOT",
    "FIELD",
    "INVALID_CODE",
    "OWN_SOURCE",
    "blob",
    "read",
    "read_many",
    "read_row",
]
