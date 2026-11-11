"""Per-track identity redaction for tracklists and now-playing (NOWP-11487).

A track whose user field ``tracklist_hidden`` is true is listed as ``ID - ID``
in every tracklist export, share timeline and SoundCloud comment, while
timestamps and ``stable_id`` stay put so overlays can still line up timing.

The now-playing resolver (epic #10335, ``apps/now_playing/resolve.py`` when it
lands) must call :func:`redact_identity` for a hidden track's answer row.

-Claude
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from apps.shared.state import db as state_db

FIELD: Final = "tracklist_hidden"
HIDDEN_LABEL: Final = "ID"


def hidden_stable_ids(conn: sqlite3.Connection) -> frozenset[str]:
    """Every stable_id whose tracklist_hidden is true.

    An empty set when ``track_fields`` is absent. Raises ``ValueError`` when a
    stored value is not a JSON bool.
    """
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='track_fields'").fetchone():
        return frozenset()
    hidden: set[str] = set()
    for row in conn.execute(
        "SELECT stable_id, value_json FROM track_fields WHERE field_name = ? AND deleted_at IS NULL",
        (FIELD,),
    ):
        value = json.loads(str(row[1]))
        if not isinstance(value, bool):
            raise TypeError(f"{FIELD} for {row[0]!r} is not a bool: {row[1]!r}")
        if value:
            hidden.add(str(row[0]))
    return frozenset(hidden)


def hidden_stable_ids_at(path: Path) -> frozenset[str]:
    """Open ``path`` read-only, return :func:`hidden_stable_ids`, then close."""
    if not path.is_file():
        raise FileNotFoundError(f"state.db missing: {path}")
    conn = state_db.open_ro(path)
    try:
        return hidden_stable_ids(conn)
    finally:
        conn.close()


def redact_identity(row: Mapping[str, Any]) -> dict[str, Any]:
    """Copy of ``row`` with artist and title set to ``ID``; every other key unchanged."""
    out = dict(row)
    out["artist"] = HIDDEN_LABEL
    out["title"] = HIDDEN_LABEL
    return out


__all__ = [
    "FIELD",
    "HIDDEN_LABEL",
    "hidden_stable_ids",
    "hidden_stable_ids_at",
    "redact_identity",
]
