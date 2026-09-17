"""Durable one-shot markers for post-migration repairs (#3171, #3165)."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

MARKER_TABLE: str = "schema_meta_markers"


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def marker_row_present(conn: sqlite3.Connection, marker: str) -> bool:
    """Raw marker lookup; the caller has already proven the table exists."""
    row = conn.execute(
        f"SELECT 1 FROM {MARKER_TABLE} WHERE marker = ?",
        (marker,),
    ).fetchone()
    return row is not None


def has_marker(conn: sqlite3.Connection, marker: str) -> bool:
    if not table_exists(conn, MARKER_TABLE):
        return False
    return marker_row_present(conn, marker)


def insert_marker(conn: sqlite3.Connection, marker: str) -> None:
    conn.execute(
        f"INSERT INTO {MARKER_TABLE}(marker, applied_at) VALUES (?, ?)",
        (marker, datetime.now(UTC).isoformat()),
    )


__all__ = [
    "MARKER_TABLE",
    "has_marker",
    "insert_marker",
    "marker_row_present",
    "table_exists",
]
