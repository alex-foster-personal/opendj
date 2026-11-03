"""SQL schema for playlist sets (SET-05) and set routes (SET-6586).

SET-05 performance objects live in their own table family, not in
``play_orders`` (PLAY-01 orderings) and not in SET-01 recorded ``sets``.
Migrated independently via ``playlist_sets_schema_meta``.

v2 (SET-6586) adds components: named, ordered runs of tracks inside a playlist,
each with a slot (opener, main, ending...) and free tags. A set may name an
ordered list of components (its route) in ``playlist_set_parts``; a v1 set has
no parts and keeps its flat ``playlist_set_entries`` snapshot. Every run
freezes the tracks it resolved to in ``playlist_set_run_entries``.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

SCHEMA_VERSION: int = 2

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

_V2: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS playlist_component_slots (
        playlist_id     TEXT NOT NULL,
        slot            TEXT NOT NULL,
        position        INTEGER NOT NULL,
        PRIMARY KEY (playlist_id, slot)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS playlist_components (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        playlist_id     TEXT NOT NULL,
        name            TEXT NOT NULL,
        slot            TEXT NOT NULL,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL,
        UNIQUE(playlist_id, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS playlist_component_entries (
        component_id    INTEGER NOT NULL
                        REFERENCES playlist_components(id) ON DELETE CASCADE,
        position        INTEGER NOT NULL,
        stable_id       TEXT NOT NULL,
        PRIMARY KEY (component_id, position)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS playlist_component_tags (
        component_id    INTEGER NOT NULL
                        REFERENCES playlist_components(id) ON DELETE CASCADE,
        tag             TEXT NOT NULL,
        PRIMARY KEY (component_id, tag)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS playlist_set_parts (
        set_id          INTEGER NOT NULL
                        REFERENCES playlist_sets(id) ON DELETE CASCADE,
        position        INTEGER NOT NULL,
        component_id    INTEGER NOT NULL
                        REFERENCES playlist_components(id) ON DELETE RESTRICT,
        PRIMARY KEY (set_id, position)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS playlist_set_run_entries (
        run_id          INTEGER NOT NULL
                        REFERENCES playlist_set_runs(id) ON DELETE CASCADE,
        position        INTEGER NOT NULL,
        stable_id       TEXT NOT NULL,
        component_id    INTEGER,
        PRIMARY KEY (run_id, position)
    )
    """,
    # A v1 run froze nothing: the set's insert-only snapshot is the only record
    # of what it played, so every existing run gets a copy of it here.
    "INSERT OR IGNORE INTO playlist_set_run_entries"
    "(run_id, position, stable_id, component_id) "
    "SELECT r.id, e.position, e.stable_id, NULL "
    "FROM playlist_set_runs r JOIN playlist_set_entries e ON e.set_id = r.set_id",
    "CREATE INDEX IF NOT EXISTS idx_playlist_components_playlist "
    "ON playlist_components(playlist_id, slot)",
    "CREATE INDEX IF NOT EXISTS idx_playlist_set_parts_component "
    "ON playlist_set_parts(component_id)",
]

_MIGRATIONS: list[list[str]] = [_V1, _V2]


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


_WRITE_UNIT = "playlist_sets_unit"


@contextmanager
def write_unit(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One all-or-nothing unit around a multi-statement playlist-set write.

    State handles are opened with ``isolation_level=None``, so without this a
    failure halfway through (a duplicate name, a bad track list) leaves the
    earlier statements committed. Nested calls use a SAVEPOINT so a caller's
    own transaction is never committed early.
    """
    nested = conn.in_transaction
    conn.execute(f"SAVEPOINT {_WRITE_UNIT}" if nested else "BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        if nested:
            conn.execute(f"ROLLBACK TO {_WRITE_UNIT}")
            conn.execute(f"RELEASE {_WRITE_UNIT}")
        else:
            conn.execute("ROLLBACK")
        raise
    conn.execute(f"RELEASE {_WRITE_UNIT}" if nested else "COMMIT")
