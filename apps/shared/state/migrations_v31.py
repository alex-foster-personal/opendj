"""Migration 30 -> 31: library HQ stems + lyric-match completeness flag (#11219).

Persists the product expectation in sqlite for a later CloudSync attach
(parent #759). Fresh installs seed false; existing library profiles seed
true without a human click.
"""

from __future__ import annotations

import sqlite3

from apps.shared.library_completeness_config import (
    CFG,
    read_expect_hq_stems_and_lyric_match,
    row_is_seeded,
    seed_expectation,
)  # CFG drives CREATE TABLE ddl for the singleton flag row

_V31: list[str] = [
    f"""
    CREATE TABLE {CFG.table} (
        id INTEGER PRIMARY KEY CHECK (id = {CFG.row_id}),
        {CFG.column} INTEGER NOT NULL
            CHECK ({CFG.column} IN (0, 1)),
        seeded_at TEXT NOT NULL
    )
    """,
]

_LIBRARY_PRESENCE_QUERIES: tuple[str, ...] = (
    "SELECT 1 FROM tracks WHERE deleted_at IS NULL LIMIT 1",
    "SELECT 1 FROM playlists WHERE deleted_at IS NULL LIMIT 1",
    "SELECT 1 FROM track_fields LIMIT 1",
    "SELECT 1 FROM adapters LIMIT 1",
)


def library_profile_exists(conn: sqlite3.Connection) -> bool:
    """True when this file already carried library state before the v31 seed."""
    for query in _LIBRARY_PRESENCE_QUERIES:
        if conn.execute(query).fetchone() is not None:
            return True
    return False


def seed_library_completeness_config(
    conn: sqlite3.Connection,
    *,
    expect: bool | None = None,
) -> None:
    """Insert the singleton cfg row when absent; leave an existing row alone."""
    if row_is_seeded(conn):
        return
    if expect is None:
        expect = library_profile_exists(conn)
    seed_expectation(conn, expect=expect)


def read_seeded_expectation(conn: sqlite3.Connection) -> bool:
    """CFG read path after migration."""
    return read_expect_hq_stems_and_lyric_match(conn)


__all__ = [
    "_V31",
    "library_profile_exists",
    "read_seeded_expectation",
    "seed_library_completeness_config",
]
