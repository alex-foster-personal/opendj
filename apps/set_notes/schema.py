"""SQL schema for set notes (SETNOTE-6340c).

One markdown note per scope string; migrated independently via
``set_notes_schema_meta``.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

SCHEMA_VERSION: int = 1

_V1: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS set_notes (
        scope       TEXT PRIMARY KEY,
        body_md     TEXT NOT NULL,
        x           INTEGER,
        y           INTEGER,
        w           INTEGER,
        h           INTEGER,
        visible     INTEGER NOT NULL,
        rev         INTEGER NOT NULL,
        updated_at  TEXT NOT NULL
    )
    """,
]

_MIGRATIONS: list[list[str]] = [_V1]


def _ensure_meta(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS set_notes_schema_meta (
            version    INTEGER PRIMARY KEY,
            applied_at TEXT    NOT NULL
        )
        """
    )


def _current_version(conn: sqlite3.Connection) -> int:
    (version,) = conn.execute("SELECT COALESCE(MAX(version), 0) FROM set_notes_schema_meta").fetchone()
    return int(version)


def apply_set_notes_migrations(conn: sqlite3.Connection) -> int:
    """Apply pending set-notes migrations; return resulting version."""
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
                "INSERT INTO set_notes_schema_meta(version, applied_at) VALUES (?, ?)",
                (target_version, datetime.now(UTC).isoformat()),
            )
            if not in_txn:
                conn.execute("COMMIT")
        except Exception:
            if not in_txn:
                conn.execute("ROLLBACK")
            raise

    return _current_version(conn)
