"""SQL schema for playlist sets (SET-05).

SET-05 performance objects live in their own table family, not in
``play_orders`` (PLAY-01 orderings) and not in SET-01 recorded ``sets``.
Migrated independently via ``playlist_sets_schema_meta``.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

SCHEMA_VERSION: int = 1

_V1: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS playlist_sets (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        playlist_id     TEXT NOT NULL,
        name            TEXT NOT NULL,
        play_count      INTEGER NOT NULL DEFAULT 0,
        source_play_order_id INTEGER,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL,
        UNIQUE(playlist_id, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS playlist_set_entries (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        set_id          INTEGER NOT NULL REFERENCES playlist_sets(id) ON DELETE CASCADE,
        stable_id       TEXT NOT NULL,
        position        INTEGER NOT NULL,
        UNIQUE(set_id, position)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS playlist_set_runs (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        set_id          INTEGER NOT NULL REFERENCES playlist_sets(id) ON DELETE CASCADE,
        kind            TEXT NOT NULL CHECK(kind IN ('practice', 'performance')),
        created_at      TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_playlist_sets_playlist "
    "ON playlist_sets(playlist_id)",
    "CREATE INDEX IF NOT EXISTS idx_playlist_set_runs_set "
    "ON playlist_set_runs(set_id, kind)",
]

_MIGRATIONS: list[list[str]] = [_V1]


def _ensure_meta(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS playlist_sets_schema_meta (
            version    INTEGER PRIMARY KEY,
            applied_at TEXT    NOT NULL
        )
        """
    )


def _current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) FROM playlist_sets_schema_meta"
    ).fetchone()
    return int(row[0]) if row is not None else 0


def apply_playlist_set_migrations(conn: sqlite3.Connection) -> int:
    """Apply pending playlist-set migrations; return resulting version."""
    _ensure_meta(conn)
    current = _current_version(conn)
    if current >= SCHEMA_VERSION:
        return current

    for step_idx in range(current, SCHEMA_VERSION):
        statements = _MIGRATIONS[step_idx]
        target_version = step_idx + 1
        in_txn = conn.in_transaction
        if not in_txn:
            conn.execute("BEGIN")
        try:
            for stmt in statements:
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO playlist_sets_schema_meta(version, applied_at) "
                "VALUES (?, ?)",
                (target_version, datetime.now(UTC).isoformat()),
            )
            if not in_txn:
                conn.execute("COMMIT")
        except Exception:
            if not in_txn:
                conn.execute("ROLLBACK")
            raise

    return _current_version(conn)
