"""Single-document mutations and the reconcile batch for the lyric-search index.

Split out of :mod:`apps.lyrics.search_index` (issue #1343, file-size ratchet)
so that module can stay under the 600-line gate. This module owns:

- inserting and removing one document (:func:`add_document`,
  :func:`remove_document`)
- one committed reconcile batch, i.e. one poll of the background job
  (:func:`index_batch` and its helpers)

Every name here is re-exported from ``apps.lyrics.search_index`` so callers
and tests never need to know the module was split. Pure move, no behavior
change.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from apps.lyrics import cache as lyrics_cache
from apps.lyrics.asr_hallucination import servable_lyrics
from apps.lyrics.search_contract import LyricSearchDocument, build_search_document
from apps.lyrics.search_index_schema import (
    LyricsCacheUnavailable,
    LyricsIndexMeta,
    count_documents,
    index_path,
    indexed_ids,
    open_write,
    read_meta,
)

#: Default ceiling on documents indexed in one batch (one poll of the job).
DEFAULT_BATCH_MAX_DOCS = 100

#: Refuse to remove more than this fraction of an existing index's rows in
#: one batch without ``force_rebuild=True``. A missing/unreadable cache dir
#: is already rejected outright (see ``LyricsCacheUnavailable``); this bounds
#: the blast radius of anything else that makes ``candidates`` look smaller
#: than ``indexed`` so a transient condition can never cost the whole index.
MAX_REMOVAL_FRACTION = 0.5

#: A readable cache entry with nothing to index: an ASR transcript of only
#: Whisper hallucinations (LYRICS-12). Not corrupt and not pending work.
NoLyrics = Literal["no-lyrics"]
_NO_LYRICS: Final[NoLyrics] = "no-lyrics"


# ----- single-document mutations ------------------------------------------


def add_document(conn: sqlite3.Connection, doc: LyricSearchDocument) -> bool:
    """Insert one document and map its rowid. False if it is already present.

    ``lyric_rows`` is the durable manifest that makes membership a plain SQL
    read, so resume never has to re-derive the indexed set by scanning the
    FTS table. ``fts_rowid`` is stored so a later removal can ``DELETE`` the
    exact FTS row instead of playing content-matching games with the token
    index.
    """
    if _has_document(conn, doc.stable_id):
        return False
    fts_rowid = int(
        conn.execute("SELECT COALESCE(MAX(fts_rowid), 0) + 1 FROM lyric_rows").fetchone()[0]
    )
    conn.execute(
        "INSERT INTO lyrics_fts(rowid, stable_id, searchable_text) VALUES (?, ?, ?)",
        (fts_rowid, doc.stable_id, doc.searchable_text),
    )
    conn.execute(
        "INSERT INTO lyric_rows(stable_id, fts_rowid, source, line_count) VALUES (?, ?, ?, ?)",
        (doc.stable_id, fts_rowid, doc.source, doc.line_count),
    )
    return True


def remove_document(conn: sqlite3.Connection, stable_id: str) -> bool:
    """Drop one document's FTS row and manifest row. False if not present."""
    row = conn.execute(
        "SELECT fts_rowid FROM lyric_rows WHERE stable_id = ?", (stable_id,)
    ).fetchone()
    if row is None:
        return False
    conn.execute("DELETE FROM lyrics_fts WHERE rowid = ?", (int(row[0]),))
    conn.execute("DELETE FROM lyric_rows WHERE stable_id = ?", (stable_id,))
    return True


def _has_document(conn: sqlite3.Connection, stable_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM lyric_rows WHERE stable_id = ?", (stable_id,)).fetchone()
    return row is not None


# ----- reconcile batches (one poll of the background job) -----------------


@dataclass(frozen=True)
class IndexBatch:
    """What one committed batch changed, and what is left.

    ``pending`` is the number of cache files on disk that still need a valid
    document in the index after this batch. Corrupt files are not pending
    work: they can never be indexed, so they are excluded once this batch has
    attempted them.
    """

    added: int
    removed: int
    corrupt: int
    docs_indexed: int
    pending: int
    done: bool
    no_lyrics: int = 0


def _cache_fingerprint(lyrics_dir: Path) -> str:
    """A cheap (single ``stat`` call) signature of the cache dir's contents.

    Cache entries are immutable once written (see module docstring), and
    adding or removing a directory entry moves that directory's own mtime on
    every filesystem this project targets - so "fingerprint unchanged" means
    "nothing to reconcile" without listing or re-parsing a single file.
    """
    stat = lyrics_dir.stat()
    return f"{stat.st_mtime_ns}:{stat.st_size}"


def index_batch(
    data_dir: Path,
    *,
    max_docs: int = DEFAULT_BATCH_MAX_DOCS,
    wall_clock: Callable[[], float] = time.time,
    force_rebuild: bool = False,
) -> IndexBatch:
    """Index up to ``max_docs`` new cache entries and drop stale rows.

    Opens the index fresh, does the whole batch in ONE transaction, commits,
    and closes - so a process that dies at any point leaves exactly the
    committed batches on disk and nothing in RAM. Cache entries already
    indexed are never re-parsed (membership is read from ``lyric_rows``, not
    by re-reading the source JSON).

    Raises :class:`LyricsCacheUnavailable` when the cache dir cannot be read
    while an index already exists, or when reconciling it would remove more
    than :data:`MAX_REMOVAL_FRACTION` of the indexed rows in one batch
    (pass ``force_rebuild=True`` to allow a real bulk removal through). Both
    guards exist so a transient mount/volume issue, or a directory moved or
    emptied mid-run, can never look like "the library has zero lyrics now"
    and wipe the whole index.
    """
    if max_docs <= 0:
        raise ValueError(f"max_docs must be > 0, got {max_docs}")
    lyrics_dir = lyrics_cache.cache_dir(data_dir)
    db_path = index_path(data_dir)
    early = _uninitialized_no_op_or_raise(lyrics_dir, db_path)
    if early is not None:
        return early

    conn = open_write(db_path)
    try:
        fingerprint = _cache_fingerprint(lyrics_dir)
        meta = read_meta(conn)
        idle = _idle_poll_result(meta, fingerprint)
        if idle is not None:
            return idle

        candidates = _on_disk_ids(lyrics_dir)
        indexed = indexed_ids(conn)
        removed = _remove_stale_rows(conn, indexed, candidates, force_rebuild=force_rebuild)
        added, corrupt, no_lyrics = _add_new_candidates(
            conn, lyrics_dir, candidates, indexed, max_docs
        )
        docs_indexed = count_documents(conn)
        pending = max(0, len(candidates) - docs_indexed - corrupt - no_lyrics)
        conn.execute(
            "UPDATE lyrics_index_meta SET docs_indexed = ?,"
            " committed_batches = committed_batches + 1,"
            " last_committed_ms = ?, cache_fingerprint = ?, last_pending = ?"
            " WHERE id = 1",
            (docs_indexed, int(wall_clock() * 1000), fingerprint, pending),
        )
        conn.commit()
    finally:
        conn.close()
    return IndexBatch(
        added=added,
        removed=removed,
        corrupt=corrupt,
        docs_indexed=docs_indexed,
        pending=pending,
        done=pending == 0,
        no_lyrics=no_lyrics,
    )


def _uninitialized_no_op_or_raise(lyrics_dir: Path, db_path: Path) -> IndexBatch | None:
    """The two "nothing to reconcile yet" cases a batch must settle before it
    ever opens the index, or None to say a real reconcile should proceed.

    Raises :class:`LyricsCacheUnavailable` when the cache dir cannot be read
    and an index already exists - see :func:`index_batch`'s docstring for why.
    """
    if not lyrics_dir.is_dir():
        if not db_path.exists():
            # Nothing on disk and no index yet: a true no-op. Do not create
            # an empty index file for a library that has no lyrics at all.
            return IndexBatch(0, 0, 0, 0, 0, True)
        # An index already exists but the cache dir cannot be read right now
        # (unmounted volume, a dir moved or removed mid-run). Treating that
        # as "zero candidates" would delete every indexed row in one
        # committed, unbounded batch - fail loud and leave the index
        # untouched instead.
        raise LyricsCacheUnavailable(f"lyrics cache dir is missing or unreadable: {lyrics_dir}")
    if not db_path.exists() and not _on_disk_ids(lyrics_dir):
        # An empty (but real, readable) cache dir and no index yet: also a
        # true no-op, distinct from the missing/unreadable case above. This
        # branch runs at most once per data dir - the first committed batch
        # creates the index file, so every later poll skips straight to the
        # idle-poll check below.
        return IndexBatch(0, 0, 0, 0, 0, True)
    return None


def _idle_poll_result(meta: LyricsIndexMeta | None, fingerprint: str) -> IndexBatch | None:
    """A finished ``IndexBatch`` when this poll can skip the rescan entirely,
    else None to say a real reconcile should proceed.

    Only short-circuits a poll that is both caught up (nothing was left
    pending after the last committed batch) AND sees the same cache-dir
    mtime/size as that batch did. ``last_pending`` alone is not enough
    (max_docs pagination leaves real work pending against an UNCHANGED dir
    between batches - that must always reconcile), and the fingerprint alone
    is not enough (it cannot tell "caught up" from "still bounded by
    max_docs"). Only the steady idle-poll state - fully caught up, dir
    unchanged since - can safely skip the rescan (glob every file,
    materialize the indexed set, re-parse every corrupt candidate) and the
    write transaction, so an idle poll costs one stat() call instead of
    repeating that work every second forever.
    """
    if meta is None or int(meta["last_pending"]) != 0 or meta["cache_fingerprint"] != fingerprint:
        return None
    return IndexBatch(
        added=0,
        removed=0,
        corrupt=0,
        docs_indexed=int(meta["docs_indexed"]),
        pending=0,
        done=True,
    )


def _remove_stale_rows(
    conn: sqlite3.Connection,
    indexed: set[str],
    candidates: set[str],
    *,
    force_rebuild: bool,
) -> int:
    """Drop indexed rows whose cache file is gone, bounded by
    :data:`MAX_REMOVAL_FRACTION` unless ``force_rebuild`` says this batch is
    a deliberate rebuild."""
    to_remove = sorted(indexed - candidates)
    if to_remove and indexed and not force_rebuild:
        limit = max(1, int(len(indexed) * MAX_REMOVAL_FRACTION))
        if len(to_remove) > limit:
            raise LyricsCacheUnavailable(
                f"batch would remove {len(to_remove)} of {len(indexed)} indexed "
                f"rows, over the {int(MAX_REMOVAL_FRACTION * 100)}% per-batch "
                "bound; pass force_rebuild=True to reconcile a real bulk removal"
            )
    removed = 0
    for stable_id in to_remove:
        if remove_document(conn, stable_id):
            removed += 1
    return removed


def _add_new_candidates(
    conn: sqlite3.Connection,
    lyrics_dir: Path,
    candidates: set[str],
    indexed: set[str],
    max_docs: int,
) -> tuple[int, int, int]:
    """Add up to ``max_docs`` new candidates. Returns ``(added, corrupt, no_lyrics)``."""
    added = 0
    corrupt = 0
    no_lyrics = 0
    budget = max_docs
    for stable_id in sorted(candidates - indexed):
        if budget <= 0:
            break
        document = _load_candidate(lyrics_dir, stable_id)
        if document is None:
            corrupt += 1
        elif isinstance(document, str):  # _NO_LYRICS
            no_lyrics += 1
        elif add_document(conn, document):
            added += 1
            budget -= 1
    return added, corrupt, no_lyrics


def _on_disk_ids(lyrics_dir: Path) -> set[str]:
    if not lyrics_dir.is_dir():
        return set()
    return {path.stem for path in lyrics_dir.glob("*.json")}


def _load_candidate(
    lyrics_dir: Path, stable_id: str
) -> LyricSearchDocument | NoLyrics | None:
    """Parse one cache file into a search document, None if corrupt, or
    ``_NO_LYRICS`` for an ASR entry that is only hallucinations (LYRICS-12).

    Mirrors ``valid_lyrics_ids``: a filename alone is not done, and an entry
    whose own ``stable_id`` disagrees with its filename is rejected too.
    """
    path = lyrics_dir / f"{stable_id}.json"
    try:
        entry = lyrics_cache.load(path)
    except (TypeError, ValueError):
        return None
    if entry is None or entry.stable_id != stable_id:
        return None
    servable = servable_lyrics(entry)
    if servable is None:
        return _NO_LYRICS
    try:
        return build_search_document(servable)
    except ValueError:
        return None


__all__ = [
    "DEFAULT_BATCH_MAX_DOCS",
    "MAX_REMOVAL_FRACTION",
    "IndexBatch",
    "add_document",
    "index_batch",
    "remove_document",
]
