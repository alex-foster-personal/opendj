"""Durable checkpointed lyric-search index (LYRICS-03, Part 2 of #935).

Every case here builds a REAL lyrics cache on disk with the production
writer (``apps.lyrics.cache.write``) and a REAL sqlite FTS5 index in a tmp
data dir. Nothing is stubbed: a batch that reports ``added`` must have a
matching row in the on-disk file a fresh connection can read.

Regression lines:
  - if index_batch indexes more than max_docs documents in one call then broken
  - if committed batches do not survive interruption (fresh connection) then broken
  - if the index resumes by re-indexing already-committed documents then broken
  - if a corrupt cache entry crashes the batch or is indexed as garbage then broken
  - if an index row outlives the cache file it was built from then broken
  - if an empty query matches everything instead of failing then broken
  - if last_committed_ms is not wall-clock epoch milliseconds then broken
  - if search_documents cannot read hits from a populated on-disk index then broken
  - if a missing/unreadable cache dir deletes indexed rows instead of raising then broken
  - if a batch removes more than the per-batch bound without force_rebuild then broken
  - if an idle poll (caught up, cache dir unchanged) rescans or re-stamps the checkpoint then broken
  - if a schema bump leaves lyrics_index_meta in its old column shape then broken
"""

from __future__ import annotations

import shutil
import sqlite3
import time
from pathlib import Path

import pytest

import apps.lyrics.search_index_batch as search_index_batch_mod
import apps.lyrics.search_index_schema as search_index_schema_mod
from apps.lyrics.cache import LyricLine, Lyrics, cache_dir, cache_path, write
from apps.lyrics.search_index import (
    LyricsCacheUnavailable,
    count_documents,
    index_batch,
    index_path,
    indexed_ids,
    open_write,
    read_meta,
    search,
    search_documents,
)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """A per-test data root with no index yet."""
    return tmp_path / "data"


def _write(data_dir: Path, stable_id: str, *line_texts: str) -> None:
    """Write one real, cache-schema lyric entry via the production writer."""
    lyrics = Lyrics(
        stable_id=stable_id,
        source="lrclib",
        lines=tuple(
            LyricLine(start_ms=index * 1000, text=text) for index, text in enumerate(line_texts)
        ),
    )
    write(cache_path(data_dir, stable_id), lyrics)


def _corrupt(data_dir: Path, stable_id: str) -> None:
    """Write a file the cache reader rejects (schema-invalid), as an
    interrupted worker would leave behind."""
    path = cache_path(data_dir, stable_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"schema": 1, "stable_id": "x", "lines": [', encoding="utf-8")


def _drain(data_dir: Path, *, max_docs: int = 100) -> None:
    """Run batches until the index reports no pending work."""
    for _ in range(1000):
        batch = index_batch(data_dir, max_docs=max_docs)
        if batch.done:
            return
    raise AssertionError("index did not drain within 1000 batches")


def test_index_batch_is_bounded_by_max_docs_per_call(data_dir: Path) -> None:
    """If a call can only do max_docs then the rest stay pending for the next batch."""
    for sid in ("a", "b", "c", "d", "e"):
        _write(data_dir, sid, f"{sid} has lyrics to sing about the moonlight")

    first = index_batch(data_dir, max_docs=2)

    assert first.added == 2
    assert first.removed == 0
    assert first.docs_indexed == 2
    assert first.pending == 3
    assert first.done is False

    second = index_batch(data_dir, max_docs=2)
    assert second.added == 2
    assert second.docs_indexed == 4

    third = index_batch(data_dir, max_docs=2)
    assert third.added == 1
    assert third.docs_indexed == 5
    assert third.pending == 0
    assert third.done is True


def test_committed_batches_survive_and_resume_from_a_fresh_connection(
    data_dir: Path,
) -> None:
    """If a run stops mid-corpus then a brand-new connection sees the committed
    docs on disk and a later run resumes past them (nothing re-indexes)."""
    for sid in ("a", "b", "c", "d", "e"):
        _write(
            data_dir,
            sid,
            f"verse one of {sid} is about moonlight and verse two of {sid} about starlight",
        )

    interrupted = index_batch(data_dir, max_docs=2)

    assert interrupted.added == 2
    # A new connection - the only state after a process interruption - reads
    # both committed docs straight off disk, not out of any in-memory object.
    conn = open_write(index_path(data_dir))
    try:
        assert count_documents(conn) == 2
        assert indexed_ids(conn) == {"a", "b"}
        meta = read_meta(conn)
        assert meta is not None
        assert meta["docs_indexed"] == 2
        assert meta["committed_batches"] == 1
        assert meta["last_committed_ms"] is not None
        # The committed half is searchable from the fresh connection too.
        # Every generated line mentions "moonlight", so two docs are hits.
        assert sorted(search(conn, "moonlight", limit=10)[0]) == ["a", "b"]
        assert search(conn, "moonlight", limit=10)[1] == 2
    finally:
        conn.close()

    # Resuming indexes only the three missing docs, not a..e again.
    resumed = index_batch(data_dir, max_docs=100)
    assert resumed.added == 3
    assert resumed.docs_indexed == 5
    conn = open_write(index_path(data_dir))
    try:
        assert count_documents(conn) == 5
        assert search(conn, "moonlight", limit=10)[1] == 5
    finally:
        conn.close()


def test_corrupt_cache_entry_is_counted_not_indexed_and_does_not_abort(
    data_dir: Path,
) -> None:
    """If one cache file is schema-invalid then the batch skips it loudly and
    still indexes every valid entry beside it."""
    _write(data_dir, "good-a", "dancing in the moonlight")
    _corrupt(data_dir, "broken")
    _write(data_dir, "good-b", "walking on sunshine")

    batch = index_batch(data_dir, max_docs=100)

    assert batch.added == 2
    assert batch.corrupt == 1
    assert batch.done is True
    conn = open_write(index_path(data_dir))
    try:
        assert indexed_ids(conn) == {"good-a", "good-b"}
    finally:
        conn.close()


def test_index_row_is_dropped_when_its_cache_file_vanishes(data_dir: Path) -> None:
    """If a track's lyrics are removed from disk then the next batch drops the
    stale index row instead of returning it as a hit."""
    _write(data_dir, "a", "dancing in the moonlight")
    _write(data_dir, "b", "walking on sunshine")
    _drain(data_dir)

    # 'a' leaves the library; its cache file goes with it.
    cache_path(data_dir, "a").unlink()

    batch = index_batch(data_dir, max_docs=100)
    assert batch.removed == 1

    conn = open_write(index_path(data_dir))
    try:
        assert search(conn, "moonlight", limit=10) == ([], 0)
        assert search(conn, "sunshine", limit=10) == (["b"], 1)
        assert batch.docs_indexed == 1
    finally:
        conn.close()


def test_missing_cache_dir_raises_and_leaves_index_untouched(data_dir: Path) -> None:
    """A cache dir that cannot be read right now (unmounted volume, moved or
    removed mid-run) must never look like 'zero candidates': that would
    delete every indexed row in one committed, unbounded batch. It must fail
    loud instead and leave the index exactly as it was."""
    _write(data_dir, "a", "dancing in the moonlight")
    _write(data_dir, "b", "walking on sunshine")
    _drain(data_dir)

    conn = open_write(index_path(data_dir))
    try:
        assert indexed_ids(conn) == {"a", "b"}
    finally:
        conn.close()

    shutil.rmtree(cache_dir(data_dir))

    with pytest.raises(LyricsCacheUnavailable, match="missing or unreadable"):
        index_batch(data_dir, max_docs=100)

    conn = open_write(index_path(data_dir))
    try:
        assert indexed_ids(conn) == {"a", "b"}
        assert count_documents(conn) == 2
    finally:
        conn.close()


def test_present_but_empty_cache_dir_removes_the_lone_indexed_row(data_dir: Path) -> None:
    """An existing, readable cache dir that is simply empty is a real,
    reconcilable state, unlike the missing/unreadable dir above: within the
    per-batch removal bound it deletes normally rather than raising."""
    _write(data_dir, "a", "dancing in the moonlight")
    _drain(data_dir)

    cache_path(data_dir, "a").unlink()
    assert cache_dir(data_dir).is_dir()
    assert list(cache_dir(data_dir).iterdir()) == []

    batch = index_batch(data_dir, max_docs=100)
    assert batch.removed == 1
    assert batch.docs_indexed == 0

    conn = open_write(index_path(data_dir))
    try:
        assert count_documents(conn) == 0
    finally:
        conn.close()


def test_bulk_removal_over_the_bound_raises_without_force_rebuild(data_dir: Path) -> None:
    """A single batch that would drop most of the index - here a real bulk
    deletion, not a missing cache dir - is bounded the same way additions
    are bounded by max_docs: it refuses rather than wiping the majority of
    the index in one commit, unless force_rebuild says this is deliberate."""
    for sid in ("a", "b", "c", "d"):
        _write(data_dir, sid, f"{sid} has lyrics to sing about the moonlight")
    _drain(data_dir)

    for sid in ("a", "b", "c"):
        cache_path(data_dir, sid).unlink()

    with pytest.raises(LyricsCacheUnavailable, match="per-batch"):
        index_batch(data_dir, max_docs=100)

    conn = open_write(index_path(data_dir))
    try:
        assert indexed_ids(conn) == {"a", "b", "c", "d"}
        assert count_documents(conn) == 4
    finally:
        conn.close()

    batch = index_batch(data_dir, max_docs=100, force_rebuild=True)
    assert batch.removed == 3
    assert batch.docs_indexed == 1

    conn = open_write(index_path(data_dir))
    try:
        assert indexed_ids(conn) == {"d"}
    finally:
        conn.close()


def test_idle_poll_skips_rescan_and_checkpoint_write_when_cache_unchanged(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caught-up poll against an unchanged cache dir must not glob the
    cache dir or touch the checkpoint row at all - the whole point of the
    idle-poll short circuit (round-2 #1343 review), not just an unchanged
    outcome that a full rescan happened to reproduce."""
    _write(data_dir, "a", "dancing in the moonlight")
    _drain(data_dir)

    conn = open_write(index_path(data_dir))
    try:
        meta_before = read_meta(conn)
    finally:
        conn.close()
    assert meta_before is not None
    assert meta_before["committed_batches"] == 1

    def _boom(*_args: object, **_kwargs: object) -> set[str]:
        raise AssertionError("idle poll must not glob the cache dir")

    monkeypatch.setattr(search_index_batch_mod, "_on_disk_ids", _boom)

    again = index_batch(data_dir, max_docs=100)
    assert again.added == 0
    assert again.removed == 0
    assert again.done is True

    conn = open_write(index_path(data_dir))
    try:
        meta_after = read_meta(conn)
    finally:
        conn.close()
    assert meta_after == meta_before


def test_repeat_batches_are_idempotent(data_dir: Path) -> None:
    """If a full index is rebuilt over unchanged cache entries then nothing is
    added twice and doc counts stay flat. A caught-up batch against an
    unchanged cache dir is the idle-poll short circuit (round-2 #1343
    review), so it does not bump committed_batches either - only a batch
    that actually reconciled something does."""
    _write(data_dir, "a", "dancing in the moonlight")
    _drain(data_dir)

    again = index_batch(data_dir, max_docs=100)

    assert again.added == 0
    assert again.removed == 0
    assert again.done is True
    conn = open_write(index_path(data_dir))
    try:
        assert count_documents(conn) == 1
        meta = read_meta(conn)
        assert meta is not None
        assert meta["committed_batches"] == 1
    finally:
        conn.close()


def test_search_matches_indexed_documents(data_dir: Path) -> None:
    """Search returns the stable ids whose lyrics match, using the same
    word-prefix tokenizer the metadata global search ships."""
    _write(data_dir, "a", "dancing in the moonlight")
    _write(data_dir, "b", "walking on sunshine")
    _write(data_dir, "c", "blue moon over my hometown")
    _drain(data_dir)

    conn = open_write(index_path(data_dir))
    try:
        # Exact word and prefix behave the same: 'moon' matches a and c.
        assert sorted(search(conn, "moon", limit=10)[0]) == ["a", "c"]
        assert sorted(search(conn, "MOON", limit=10)[0]) == ["a", "c"]
        assert search(conn, "danc", limit=10) == (["a"], 1)
        # Multiple terms are ANDed at the word level.
        assert search(conn, "walking sunshine", limit=10) == (["b"], 1)
        assert search(conn, "moon hometown", limit=10) == (["c"], 1)
        assert search(conn, "zzzzz", limit=10) == ([], 0)
        # The reference scan Part 1 ships is untouched.
        assert search(conn, "walking", limit=10) == (["b"], 1)
    finally:
        conn.close()


def test_search_rejects_a_blank_query(data_dir: Path) -> None:
    """If the query is blank then the index search fails fast like the
    Part 1 reference scan does, rather than matching everything."""
    _write(data_dir, "a", "dancing in the moonlight")
    _drain(data_dir)

    conn = open_write(index_path(data_dir))
    try:
        with pytest.raises(ValueError, match="must not be empty"):
            search(conn, "   ", limit=10)
    finally:
        conn.close()


def test_search_documents_returns_empty_when_no_index_exists_yet(
    data_dir: Path,
) -> None:
    """If no batch has ever run there is no index file, which is the honest
    'nothing has been indexed' answer, not an error."""
    _write(data_dir, "a", "dancing in the moonlight")

    assert search_documents(data_dir, "moonlight", limit=10) == ([], 0)
    assert index_path(data_dir).exists() is False


def test_schema_version_bump_rebuilds_an_older_index_empty(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the index schema version moves on, an older on-disk index is dropped
    and rebuilt empty rather than half-trusted or left without its tables."""
    _write(data_dir, "a", "dancing in the moonlight")
    _drain(data_dir)
    seeded = open_write(index_path(data_dir))
    try:
        assert count_documents(seeded) == 1
    finally:
        seeded.close()

    monkeypatch.setattr(search_index_schema_mod, "LYRICS_INDEX_SCHEMA", 99)
    conn = open_write(index_path(data_dir))
    try:
        assert count_documents(conn) == 0
        meta = read_meta(conn)
        assert meta is not None
        assert meta["schema"] == 99
    finally:
        conn.close()

    # Back on the released schema the emptied store is usable again, and the
    # next batch re-indexes from the real cache that is still on disk.
    monkeypatch.setattr(search_index_schema_mod, "LYRICS_INDEX_SCHEMA", 1)
    batch = index_batch(data_dir, max_docs=100)
    assert batch.added == 1


def test_schema_bump_migrates_an_old_shaped_meta_table(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A DELETE-only reset leaves lyrics_index_meta in its OLD column shape
    forever, because the CREATE TABLE IF NOT EXISTS that follows is then a
    no-op against a table that still exists. This pins that the
    schema-mismatch path actually DROPs and recreates the table, so a real
    column change - this project's own v1->v2 bump added
    cache_fingerprint/last_pending - really migrates (issue #1343 round-3
    review)."""
    db_path = index_path(data_dir)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(str(db_path))
    try:
        # The pre-v2 shape: no cache_fingerprint/last_pending columns at all.
        raw.execute(
            "CREATE TABLE lyrics_index_meta ("
            "id INTEGER PRIMARY KEY CHECK (id = 1),"
            "schema INTEGER NOT NULL,"
            "created_ms INTEGER NOT NULL,"
            "last_committed_ms INTEGER,"
            "committed_batches INTEGER NOT NULL DEFAULT 0,"
            "docs_indexed INTEGER NOT NULL DEFAULT 0"
            ")"
        )
        raw.execute(
            "INSERT INTO lyrics_index_meta(id, schema, created_ms) VALUES (1, 1, ?)",
            (int(time.time() * 1000),),
        )
        raw.commit()
    finally:
        raw.close()

    monkeypatch.setattr(search_index_schema_mod, "LYRICS_INDEX_SCHEMA", 2)
    conn = open_write(db_path)
    try:
        # A DELETE-only reset would leave the old column set in place, and
        # read_meta's SELECT of cache_fingerprint/last_pending would raise
        # sqlite3.OperationalError: no such column. Reading it back cleanly
        # is the proof the table was actually recreated under the new shape.
        meta = read_meta(conn)
        assert meta is not None
        assert meta["schema"] == 2
        assert meta["cache_fingerprint"] is None
        assert meta["last_pending"] == 0
    finally:
        conn.close()


def test_last_committed_ms_is_wall_clock_epoch_ms_and_survives_reopen(
    data_dir: Path,
) -> None:
    """If last_committed_ms is monotonic (an arbitrary origin) rather than
    epoch milliseconds then it is unusable for staleness math and cannot be
    compared across a process restart. Pin the unit directly against
    time.time(), and pin that a fresh connection reads back the same value
    a later no-op batch left untouched."""
    _write(data_dir, "a", "dancing in the moonlight")

    before_ms = time.time() * 1000
    index_batch(data_dir, max_docs=100)
    after_ms = time.time() * 1000

    conn = open_write(index_path(data_dir))
    try:
        meta = read_meta(conn)
        assert meta is not None
        last_committed_ms = meta["last_committed_ms"]
        assert last_committed_ms is not None
        # Epoch ms, not monotonic ms (which would be some small number of
        # seconds-since-boot on this or any machine): within the wall-clock
        # window the batch actually ran in.
        assert before_ms - 1000 <= last_committed_ms <= after_ms + 1000
    finally:
        conn.close()

    # A fresh connection (the only state after a process restart) reads
    # back the identical checkpoint: nothing re-stamps it just by reopening.
    reopened = open_write(index_path(data_dir))
    try:
        meta_again = read_meta(reopened)
        assert meta_again is not None
        assert meta_again["last_committed_ms"] == last_committed_ms
    finally:
        reopened.close()

    # A later no-op batch (caught up, cache dir unchanged) really does leave
    # last_committed_ms untouched, not just an unexercised claim: run one and
    # pin it, rather than only reopening the connection above.
    no_op = index_batch(data_dir, max_docs=100)
    assert no_op.added == 0
    assert no_op.removed == 0

    after_no_op = open_write(index_path(data_dir))
    try:
        meta_after_no_op = read_meta(after_no_op)
        assert meta_after_no_op is not None
        assert meta_after_no_op["last_committed_ms"] == last_committed_ms
    finally:
        after_no_op.close()


def test_search_documents_finds_hits_in_a_populated_index(data_dir: Path) -> None:
    """The only test of search_documents' real read path (every other test
    of it exercises the missing-file branch): build a small index the normal
    way, then search it back through search_documents and get real hits."""
    _write(data_dir, "a", "dancing in the moonlight")
    _write(data_dir, "b", "walking on sunshine")
    _drain(data_dir)

    hits, total = search_documents(data_dir, "moonlight", limit=10)

    assert hits == ["a"]
    assert total == 1
