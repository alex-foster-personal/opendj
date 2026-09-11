"""Probe whether a shared-state table carries ``deleted_at`` on this connection."""
from __future__ import annotations

import sqlite3

_SOFT_DELETE_TABLES: frozenset[str] = frozenset(
    {"tracks", "playlists", "playlist_memberships"}
)


def has_soft_deletes(conn: sqlite3.Connection, table: str) -> bool:
    """Whether ``table`` in THIS connection carries ``deleted_at``.

    ``deleted_at`` arrives with shared-state schema v7. RO openers never
    migrate, so a v6 file has the table and not the column. ``table`` is
    whitelisted because ``PRAGMA table_info`` cannot take a bound parameter.
    """
    if table not in _SOFT_DELETE_TABLES:
        raise ValueError(f"unsupported table for soft-delete probe: {table!r}")
    return any(
        row[1] == "deleted_at"
        for row in conn.execute(f"PRAGMA table_info({table})")
    )


__all__ = ["has_soft_deletes"]
