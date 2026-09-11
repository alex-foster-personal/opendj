"""Durable, checkpointed lyric-search index (Part 2 of #935, issue #1343).

Builds a searchable index of the flattened lyric documents the Part 1
boundary defines (:mod:`apps.lyrics.search_contract`,
``build_search_document``) into a dedicated SQLite file next to the lyrics
cache (``data/state/lyrics-index.db``), one bounded batch per call. The
background job (:class:`apps.lyrics.index_job.LyricIndexJob`) runs one batch
per poll and yields to the UI between batches, so indexing pauses within one
poll of the UI becoming active and resumes from the on-disk checkpoint once
it goes quiet again. No lyric-result UI is built here (Part 3, #1344).

Why the partial index never lives in RAM: the index is an FTS5 virtual table
inside that SQLite file, never a Python object. Each committed batch is one
transaction flushed on ``commit()``; between batches the only document in
memory is the one currently being inserted. There is no partially-built
in-memory structure to retain while paused.

The reconcile source is the on-disk lyric cache (``apps.lyrics.cache``),
whose entries are immutable once written (``LyricsService.fetch`` never
overwrites a cache hit), so the delta is exactly: index valid cache entries
not yet indexed, drop index rows whose cache file vanished. A cache entry
that fails to parse is counted as corrupt, never indexed, and never crashes
the batch - the same corrupt semantics ``valid_lyrics_ids()`` in
``apps/webui/server/routes/ingest_job.py`` uses.

The FTS5 shape mirrors the shipped metadata global search
(``apps/webui/server/search_index.py``): a ``unicode61`` word-prefix match
(``"term"*``) so queries behave like the existing global search and stay
fast over lyric-length text. ``apps.lyrics.search_contract.find_matches`` is
unchanged: it remains the reference linear scan the LYRICS-02 latency KPI
measures, while this module is what Part 3 queries.

The index location/schema/checkpoint layer lives in
:mod:`apps.lyrics.search_index_schema`, and the single-document mutations and
reconcile batch live in :mod:`apps.lyrics.search_index_batch` (split for the
file-size ratchet, issue #1343; pure move, no behavior change). Both are
re-exported here so this stays the one import path callers and tests use.

Requirements (mini-PRD):
  ✔︎ ✅ index_batch(): index up to ``max_docs`` new cache entries and drop stale
    rows in one committed transaction.
    [if] more entries are pending than ``max_docs`` [then] exactly ``max_docs``
      are added and the rest stay pending for the next batch ⛔️
    [if] a cache file vanishes after being indexed [then] its index row is
      dropped in the next batch ⛔️
    [if] a cache entry fails to parse [then] it is counted corrupt, never
      indexed, and does not abort the batch ⛔️
  ✔︎ ✅ safety: a missing cache dir or a bulk removal never wipes the index.
    [if] the cache dir is missing or unreadable and an index already exists
      [then] the batch raises ``LyricsCacheUnavailable`` and the index is
      left untouched ⛔️
    [if] the cache dir has genuinely lost more than the per-batch removal
      bound of the indexed rows [then] the batch raises instead of deleting
      them, unless ``force_rebuild=True`` ⛔️
    [if] the cache dir's mtime/size fingerprint is unchanged since the last
      committed batch [then] the batch does no rescan and leaves the
      checkpoint untouched ⛔️
  ✔︎ ✅ durable(): committed batches live in the SQLite file and are visible to a
    fresh connection after an interruption.
    [if] a batch is committed and the process ends [then] a new connection sees
      exactly the committed documents and the next batch resumes past them ⛔️
  ✔︎ ✅ search(): word-prefix FTS5 match over the indexed documents.
    [if] the query is blank [then] the search raises rather than matching
      everything ⛔️
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from apps.lyrics.search_index_batch import (
    DEFAULT_BATCH_MAX_DOCS,
    MAX_REMOVAL_FRACTION,
    IndexBatch,
    add_document,
    index_batch,
    remove_document,
)
from apps.lyrics.search_index_schema import (
    INDEX_FILENAME,
    LYRICS_INDEX_SCHEMA,
    LyricsCacheUnavailable,
    LyricsIndexMeta,
    count_documents,
    index_path,
    indexed_ids,
    open_write,
    read_meta,
)

DEFAULT_SEARCH_LIMIT = 50
MAX_SEARCH_LIMIT = 200


# ----- search over the committed index ------------------------------------


def lyric_fts_query(query: str) -> str | None:
    """Tokenize a query into the FTS5 prefix expression, or None when blank.

    Same convention as the metadata global search: strip double quotes, then
    AND every whitespace-split term as a quoted prefix (``"term"*``) so stray
    punctuation cannot be parsed as FTS5 query syntax.
    """
    cleaned = query.strip().replace('"', "")
    terms = [term for term in cleaned.split() if term]
    if not terms:
        return None
    return " ".join(f'"{term}"*' for term in terms)


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = DEFAULT_SEARCH_LIMIT,
    offset: int = 0,
) -> tuple[list[str], int]:
    """Return ``([stable_id, ...], total_matches)`` over the committed index.

    Matches are word-prefix over the flattened ``searchable_text`` and ranked
    by FTS5 bm25. A blank query raises rather than matching everything, the
    same fail-fast the Part 1 reference scan applies.
    """
    if not (1 <= limit <= MAX_SEARCH_LIMIT):
        raise ValueError(f"limit must be in [1, {MAX_SEARCH_LIMIT}], got {limit}")
    if offset < 0:
        raise ValueError(f"offset must be >= 0, got {offset}")
    expression = lyric_fts_query(query)
    if expression is None:
        raise ValueError("search query must not be empty")
    total = int(
        conn.execute(
            "SELECT COUNT(*) FROM lyrics_fts WHERE lyrics_fts MATCH ?",
            (expression,),
        ).fetchone()[0]
    )
    rows = conn.execute(
        "SELECT stable_id FROM lyrics_fts WHERE lyrics_fts MATCH ?"
        " ORDER BY bm25(lyrics_fts) LIMIT ? OFFSET ?",
        (expression, limit, offset),
    ).fetchall()
    return [str(row[0]) for row in rows], total


def search_documents(
    data_dir: Path,
    query: str,
    *,
    limit: int = DEFAULT_SEARCH_LIMIT,
    offset: int = 0,
) -> tuple[list[str], int]:
    """Open the committed index and search it. Never writes.

    An index file that does not exist yet means nothing has ever been indexed,
    which is the honest ``([], 0)`` answer rather than an error.

    A plain (non-``mode=ro``) connection is used deliberately: the index file
    is left in WAL journal mode by ``open_write``, and a ``mode=ro`` reader
    needs write access to the data directory to create the ``-shm`` file a
    WAL reader negotiates locks through. That file is normally absent right
    after the writer closes cleanly, so a strict read-only connection fails
    with "attempt to write a readonly database" whenever this process lacks
    write permission on the directory - which the daemon that produced the
    index otherwise has. ``search()`` only ever issues SELECT statements, so
    this connection is read-only in practice without depending on SQLite's
    URI-level enforcement of it.
    """
    db_path = index_path(data_dir)
    if not db_path.is_file():
        return [], 0
    conn = sqlite3.connect(str(db_path))
    try:
        return search(conn, query, limit=limit, offset=offset)
    finally:
        conn.close()


__all__ = [
    "DEFAULT_BATCH_MAX_DOCS",
    "DEFAULT_SEARCH_LIMIT",
    "INDEX_FILENAME",
    "LYRICS_INDEX_SCHEMA",
    "MAX_REMOVAL_FRACTION",
    "MAX_SEARCH_LIMIT",
    "IndexBatch",
    "LyricsCacheUnavailable",
    "LyricsIndexMeta",
    "add_document",
    "count_documents",
    "index_batch",
    "index_path",
    "indexed_ids",
    "lyric_fts_query",
    "open_write",
    "read_meta",
    "remove_document",
    "search",
    "search_documents",
]
