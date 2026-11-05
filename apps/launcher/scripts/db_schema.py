"""Shared SQLite DDL for the Hyper-K launcher palette DB.

Used by ``bootstrap_db.py`` (production bootstrap / migration) and
``latency_check.py`` (local FTS latency fixture). Keeps the tracks,
FTS5, and frecency shapes identical so Rust search and Python tests
see one schema.

``tracks_fts`` is contentless FTS5: the ``tags`` column has no matching
``tracks`` column (reserved for Phase 6 tag unification), so callers
populate it explicitly via ``BACKFILL_TRACKS_FTS_SQL``.
"""

from __future__ import annotations

FTS5_COLUMN_LIST = "title, artist, album, genre, key, tags"
FTS5_TOKENIZE = "tokenize='unicode61 remove_diacritics 2'"

TRACKS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS tracks (
    stable_id    TEXT PRIMARY KEY,
    path         TEXT NOT NULL,
    title        TEXT,
    artist       TEXT,
    album        TEXT,
    genre        TEXT,
    key          TEXT,
    bpm          REAL,
    duration_ms  INTEGER,
    isrc         TEXT,
    source       TEXT
);
"""

TRACKS_FRECENCY_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS tracks_frecency (
    stable_id        TEXT PRIMARY KEY REFERENCES tracks(stable_id),
    plays            INTEGER DEFAULT 0,
    drags            INTEGER DEFAULT 0,
    last_played_at   INTEGER,
    last_dragged_at  INTEGER
);
"""

FRECENCY_DRAGS_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_frecency_drags
    ON tracks_frecency(drags DESC, last_dragged_at DESC);
"""


def tracks_fts_virtual_ddl(*, if_not_exists: bool = True) -> str:
    """DDL for the contentless ``tracks_fts`` FTS5 virtual table."""
    exists = "IF NOT EXISTS " if if_not_exists else ""
    return f"""
CREATE VIRTUAL TABLE {exists}tracks_fts USING fts5(
    {FTS5_COLUMN_LIST},
    {FTS5_TOKENIZE}
);
"""


def tracks_frecency_plain_ddl() -> str:
    """``tracks_frecency`` without FK (Phase 5 shared-state migration path)."""
    return """
CREATE TABLE IF NOT EXISTS tracks_frecency (
    stable_id        TEXT PRIMARY KEY,
    plays            INTEGER DEFAULT 0,
    drags            INTEGER DEFAULT 0,
    last_played_at   INTEGER,
    last_dragged_at  INTEGER
);
"""


BACKFILL_TRACKS_FTS_SQL = """
INSERT INTO tracks_fts(rowid, title, artist, album, genre, key, tags)
SELECT rowid, title, artist, album, genre, key, '' FROM tracks
"""


def bootstrap_executescript() -> str:
    """Full idempotent schema for ``launcher-bootstrap.sqlite`` rebuilds."""
    return (
        TRACKS_TABLE_DDL
        + tracks_fts_virtual_ddl(if_not_exists=True)
        + TRACKS_FRECENCY_TABLE_DDL
        + FRECENCY_DRAGS_INDEX_DDL
    )


BENCH_TRACKS_TABLE_DDL = """
CREATE TABLE tracks (
    stable_id    TEXT PRIMARY KEY,
    path         TEXT NOT NULL,
    title        TEXT,
    artist       TEXT,
    album        TEXT,
    genre        TEXT,
    key          TEXT,
    bpm          REAL,
    duration_ms  INTEGER,
    isrc         TEXT,
    source       TEXT
);
"""

BENCH_TRACKS_FRECENCY_DDL = """
CREATE TABLE tracks_frecency (
    stable_id        TEXT PRIMARY KEY,
    plays            INTEGER DEFAULT 0,
    drags            INTEGER DEFAULT 0,
    last_played_at   INTEGER,
    last_dragged_at  INTEGER
);
"""


def bench_fixture_executescript() -> str:
    """Non-idempotent schema for ephemeral latency-check fixtures."""
    return BENCH_TRACKS_TABLE_DDL + tracks_fts_virtual_ddl(if_not_exists=False) + BENCH_TRACKS_FRECENCY_DDL
