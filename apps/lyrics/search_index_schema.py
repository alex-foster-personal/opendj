"""Index location, schema, and checkpoint reads for the lyric-search index.

Split out of :mod:`apps.lyrics.search_index` (issue #1343, file-size ratchet)
so that module can stay under the 600-line gate. This module owns:

- where the index file lives (:func:`index_path`)
- the on-disk schema, including the drop-and-rebuild migration a schema bump
  triggers (:func:`open_write`, :func:`_create_schema`)
- the checkpoint row a batch reads and updates (:class:`LyricsIndexMeta`,
  :func:`read_meta`) and the plain membership reads a batch needs
  (:func:`count_documents`, :func:`indexed_ids`)

Every name here is re-exported from ``apps.lyrics.search_index`` so callers
and tests never need to know the module was split. Pure move, no behavior
change.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import TypedDict

#: Bump to force a full rebuild of an on-disk index whose shape changed.
#: v2 adds the ``cache_fingerprint``/``last_pending`` checkpoint columns an
#: idle poll needs to skip a rescan safely (issue #1343 round-2 review).
#: v3 rebuilds documents without Whisper hallucination lines (LYRICS-12).
LYRICS_INDEX_SCHEMA = 3

INDEX_FILENAME = "lyrics-index.db"


class LyricsCacheUnavailable(RuntimeError):
    """The lyrics cache directory could not be read for a reconcile batch.

    Distinct from "the cache dir exists and is empty": that is zero
    candidates, a legitimate state to reconcile against. This is "we do not
    know what is in the cache right now" (missing, unmounted, permission
    denied, or a bulk removal past the per-batch bound) and must never be
    treated as "zero candidates", or a transient filesystem condition would
    delete every indexed row in one committed, unbounded batch.
    """


# ----- index location and schema ------------------------------------------


def index_path(data_dir: Path) -> Path:
    """The index file for ``data_dir``, a sibling of the ``lyrics-cache`` dir.

    ``data_dir`` is the same root ``apps.lyrics.cache.cache_dir`` derives the
    cache from, so the two always agree on one library.
    """
    return data_dir / "state" / INDEX_FILENAME


def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS lyrics_index_meta ("
        "id INTEGER PRIMARY KEY CHECK (id = 1),"
        "schema INTEGER NOT NULL,"
        "created_ms INTEGER NOT NULL,"
        "last_committed_ms INTEGER,"
        "committed_batches INTEGER NOT NULL DEFAULT 0,"
        "docs_indexed INTEGER NOT NULL DEFAULT 0,"
        "cache_fingerprint TEXT,"
        "last_pending INTEGER NOT NULL DEFAULT 0"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS lyric_rows ("
        "stable_id TEXT PRIMARY KEY,"
        "fts_rowid INTEGER NOT NULL UNIQUE,"
        "source TEXT NOT NULL,"
        "line_count INTEGER NOT NULL"
        ")"
    )
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS lyrics_fts USING fts5("
        "stable_id UNINDEXED, searchable_text,"
        " tokenize='unicode61 remove_diacritics 2')"
    )


def _create_schema(conn: sqlite3.Connection) -> None:
    _ensure_tables(conn)
    row = conn.execute("SELECT schema FROM lyrics_index_meta WHERE id = 1").fetchone()
    if row is not None and int(row[0]) != LYRICS_INDEX_SCHEMA:
        # An index built by an older schema cannot be trusted: drop it and
        # rebuild empty, so every table is recreated under the new shape.
        # Python's sqlite3 only opens an implicit transaction for DML, so the
        # DROP statements below would otherwise autocommit immediately and
        # outlive a rollback of the recreate that follows them - an explicit
        # BEGIN makes the whole drop+recreate one atomic unit.
        conn.execute("BEGIN")
        try:
            conn.execute("DROP TABLE IF EXISTS lyrics_fts")
            conn.execute("DROP TABLE IF EXISTS lyric_rows")
            # DROP, not DELETE: a DELETE leaves the existing table's shape in
            # place, so a schema bump that adds/renames a lyrics_index_meta
            # column (this bump's own reason to exist, see LYRICS_INDEX_SCHEMA
            # above) never actually migrates - _ensure_tables' CREATE TABLE IF
            # NOT EXISTS is then a no-op against the old shape (issue #1343
            # round-3 review).
            conn.execute("DROP TABLE IF EXISTS lyrics_index_meta")
            _ensure_tables(conn)
            conn.execute(
                "INSERT INTO lyrics_index_meta(id, schema, created_ms) VALUES (1, ?, ?)",
                (LYRICS_INDEX_SCHEMA, int(time.time() * 1000)),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return
    if row is None:
        conn.execute(
            "INSERT OR IGNORE INTO lyrics_index_meta(id, schema, created_ms) VALUES (1, ?, ?)",
            (LYRICS_INDEX_SCHEMA, int(time.time() * 1000)),
        )


def open_write(db_path: Path) -> sqlite3.Connection:
    """Open the index for writing, creating the file and schema if needed."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode = WAL")
    _create_schema(conn)
    return conn


# ----- document membership and checkpoint reads ---------------------------


def count_documents(conn: sqlite3.Connection) -> int:
    """How many documents are committed in this index right now."""
    return int(conn.execute("SELECT COUNT(*) FROM lyric_rows").fetchone()[0])


def indexed_ids(conn: sqlite3.Connection) -> set[str]:
    """stable ids whose document is already committed in this index."""
    return {str(row[0]) for row in conn.execute("SELECT stable_id FROM lyric_rows").fetchall()}


class LyricsIndexMeta(TypedDict):
    """The checkpoint row, one field per column of ``lyrics_index_meta``.

    A plain ``dict[str, int | str | None]`` return type would force every
    reader to narrow a union on each field it touches (mypy cannot tell
    ``last_committed_ms`` apart from ``cache_fingerprint`` through a shared
    value type); this pins each field's real type instead.
    """

    schema: int
    created_ms: int
    last_committed_ms: int | None
    committed_batches: int
    docs_indexed: int
    cache_fingerprint: str | None
    last_pending: int


def read_meta(conn: sqlite3.Connection) -> LyricsIndexMeta | None:
    """The checkpoint row: schema, timestamps, committed-batch and doc counts.

    ``cache_fingerprint``/``last_pending`` are the idle-poll short-circuit
    state ``index_batch`` uses to skip a rescan when the cache dir has not
    changed since the last committed batch.
    """
    row = conn.execute(
        "SELECT schema, created_ms, last_committed_ms, committed_batches,"
        " docs_indexed, cache_fingerprint, last_pending"
        " FROM lyrics_index_meta WHERE id = 1"
    ).fetchone()
    if row is None:
        return None
    return {
        "schema": int(row[0]),
        "created_ms": int(row[1]),
        "last_committed_ms": None if row[2] is None else int(row[2]),
        "committed_batches": int(row[3]),
        "docs_indexed": int(row[4]),
        "cache_fingerprint": None if row[5] is None else str(row[5]),
        "last_pending": int(row[6]),
    }


__all__ = [
    "INDEX_FILENAME",
    "LYRICS_INDEX_SCHEMA",
    "LyricsCacheUnavailable",
    "LyricsIndexMeta",
    "count_documents",
    "index_path",
    "indexed_ids",
    "open_write",
    "read_meta",
]
