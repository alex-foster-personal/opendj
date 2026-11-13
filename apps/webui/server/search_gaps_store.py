"""Zero-result library searches as a browsable gap list (LIBM-87b, issue #12321).

A sidecar sqlite file next to state.db (`<state dir>/search-gaps.db`), the same
placement as `audio_quality_store`: machine-local, never synced, no state.db
migration. One row per normalized query; a repeat bumps `hit_count` and
`last_searched_at` instead of adding a row.

MINI-PRD
--------
  record_zero_result(): only non-blank queries, deduplicated by normalized text.
    [if] the query is blank after normalizing [then] nothing is written
    [if] the same query is recorded again [then] one row, hit_count + 1
  list_gaps(): newest first, never creates the file.
    [if] the store does not exist [then] an empty list and no file is made

-Claude
"""

from __future__ import annotations

import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

STORE_FILENAME = "search-gaps.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS search_gaps (
    normalized_query  TEXT PRIMARY KEY,
    query             TEXT NOT NULL,
    hit_count         INTEGER NOT NULL DEFAULT 1,
    first_searched_at TEXT NOT NULL,
    last_searched_at  TEXT NOT NULL
)
"""


@dataclass(frozen=True, slots=True)
class SearchGap:
    """One logged zero-result search."""

    normalized_query: str
    query: str
    hit_count: int
    first_searched_at: str
    last_searched_at: str


def store_path(state_db: Path) -> Path:
    """The sidecar beside the app's own `state_db`."""
    return state_db.parent / STORE_FILENAME


def normalize_query(q: str) -> str:
    """NFC, casefolded, whitespace-collapsed form used as the dedup key."""
    return " ".join(unicodedata.normalize("NFC", q).casefold().split())


def record_zero_result(q: str, state_db: Path, *, now: str | None = None) -> None:
    """Upsert one zero-result search; blank queries are never logged."""
    key = normalize_query(q)
    if not key:
        return
    stamp = now or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    path = store_path(state_db)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        with conn:
            conn.execute(_SCHEMA)
            conn.execute(
                "INSERT INTO search_gaps (normalized_query, query, hit_count, "
                "first_searched_at, last_searched_at) VALUES (?, ?, 1, ?, ?) "
                "ON CONFLICT(normalized_query) DO UPDATE SET "
                "hit_count = hit_count + 1, query = excluded.query, "
                "last_searched_at = excluded.last_searched_at",
                (key, " ".join(q.split()), stamp, stamp),
            )
    finally:
        conn.close()


def list_gaps(state_db: Path, *, limit: int = 200) -> list[SearchGap]:
    """Logged gaps, most recently searched first; empty when no store exists."""
    path = store_path(state_db)
    if not path.is_file():
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT normalized_query, query, hit_count, first_searched_at, last_searched_at "
            "FROM search_gaps ORDER BY last_searched_at DESC, normalized_query LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [SearchGap(*row) for row in rows]


__all__ = ["SearchGap", "list_gaps", "normalize_query", "record_zero_result", "store_path"]
