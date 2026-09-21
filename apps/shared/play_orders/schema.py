"""SQL schema for play-orders (Phase 13 PLAY-01 / PLAY-03).

The tables live in the same SQLite file as the Phase 5 shared-state DB
but are migrated independently via a private ``play_orders_schema_meta``
row so we do not race Phase 5's ``schema_meta`` counter. This lets the
two phases land in either order and keeps the table shape stable for
Phase 15 open-dj ratification.

Schema shape matches open-dj v0 strawman §4.6 (``PlayOrder``).

Idempotent: :func:`apply_play_order_migrations` is safe to call at every
process start.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

SCHEMA_VERSION: int = 1

_V1: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS play_orders (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        playlist_id     TEXT NOT NULL,
        name            TEXT NOT NULL,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL,
        generated_by    TEXT,
        goal_json       TEXT,
        schema_version  INTEGER NOT NULL DEFAULT 1,
        UNIQUE(playlist_id, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS play_order_entries (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        play_order_id   INTEGER NOT NULL REFERENCES play_orders(id) ON DELETE CASCADE,
        stable_id       TEXT NOT NULL,
        position        INTEGER NOT NULL,
        target_key      TEXT,
        target_tempo    REAL,
        key_sync        INTEGER,
        transition_hint TEXT,
        UNIQUE(play_order_id, position)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_play_order_entries_stable_id "
    "ON play_order_entries(stable_id)",
    "CREATE INDEX IF NOT EXISTS idx_play_orders_playlist "
    "ON play_orders(playlist_id)",
]

_MIGRATIONS: list[list[str]] = [_V1]


def _ensure_meta(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS play_orders_schema_meta (
            version    INTEGER PRIMARY KEY,
            applied_at TEXT    NOT NULL
        )
        """
    )


def _current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) FROM play_orders_schema_meta"
    ).fetchone()
    return int(row[0]) if row is not None else 0


def apply_play_order_migrations(conn: sqlite3.Connection) -> int:
    """Apply pending play-order migrations against ``conn``; return the
    resulting version. Safe to call more than once.

    Uses its own ``play_orders_schema_meta`` counter so the call does not
    contend with Phase 5's ``schema_meta``. Each migration step runs in
    its own explicit transaction; the caller's autocommit / isolation
    setting is preserved on exit.
    """
    _ensure_meta(conn)
    current = _current_version(conn)
    if current >= SCHEMA_VERSION:
        return current

    for step_idx in range(current, SCHEMA_VERSION):
        statements = _MIGRATIONS[step_idx]
        target_version = step_idx + 1
        # isolation_level may be ``None`` (autocommit) or a string; wrap
        # DDL in an explicit transaction either way.
        in_txn = conn.in_transaction
        if not in_txn:
            conn.execute("BEGIN")
        try:
            for stmt in statements:
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO play_orders_schema_meta(version, applied_at) "
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
