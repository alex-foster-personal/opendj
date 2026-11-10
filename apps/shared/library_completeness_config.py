"""Library completeness expectation stored in state.db (issue #11219, parent #759).

When enabled, the product expects every track that can have HQ stems and a
lyric match to have both. The CFG layer names the single flag; sqlite holds
the authoritative value beside the shared migration ladder.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .state.sync_stamp import canonical_now

TABLE: str = "library_completeness_config"
ROW_ID: int = 1
COLUMN: str = "expect_hq_stems_and_lyric_match"


@dataclass(frozen=True)
class CFG:
    """Config-layer names for the library completeness flag."""

    table: str = TABLE
    row_id: int = ROW_ID
    column: str = COLUMN
    default: bool = False


CFG = CFG()


def read_expect_hq_stems_and_lyric_match(conn: sqlite3.Connection) -> bool:
    """Return the stored expectation; absent row is false, never true."""
    row = conn.execute(
        f"SELECT {COLUMN} FROM {TABLE} WHERE id = ?",
        (ROW_ID,),
    ).fetchone()
    if row is None:
        return CFG.default
    value = row[0]
    if value not in (0, 1):
        raise ValueError(f"{TABLE}.{COLUMN} must be 0 or 1, got {value!r}")
    return bool(value)


def row_is_seeded(conn: sqlite3.Connection) -> bool:
    return conn.execute(f"SELECT 1 FROM {TABLE} WHERE id = ?", (ROW_ID,)).fetchone() is not None


def seed_expectation(conn: sqlite3.Connection, *, expect: bool) -> None:
    """Insert the singleton row once; refuse to overwrite an existing row."""
    if row_is_seeded(conn):
        return
    conn.execute(
        f"""
        INSERT INTO {TABLE}(id, {COLUMN}, seeded_at)
        VALUES (?, ?, ?)
        """,
        (ROW_ID, 1 if expect else 0, canonical_now()),
    )
