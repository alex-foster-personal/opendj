"""Fuzzy lyric also-search over the committed index (issue #11145)."""

from __future__ import annotations

import sqlite3
from difflib import SequenceMatcher
from pathlib import Path

import pytest

from apps.lyrics import fuzzy_match
from apps.lyrics.cache import LyricLine, Lyrics, cache_path, write
from apps.lyrics.fuzzy_match import (
    LYRIC_FUZZY_SIMILARITY_THRESHOLD,
    best_lyric_line,
    lyric_text_matches_fuzzy,
    merge_fts_and_fuzzy,
)
from apps.lyrics.search_index import index_batch, search_documents
from apps.lyrics.search_snippet import matched_snippet


def _write(data_dir: Path, stable_id: str, *line_texts: str) -> None:
    lyrics = Lyrics(
        stable_id=stable_id,
        source="lrclib",
        lines=tuple(LyricLine(start_ms=index * 1000, text=text) for index, text in enumerate(line_texts)),
    )
    write(cache_path(data_dir, stable_id), lyrics)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.mark.requirement("SEARCH-NAV-11966")
def test_exact_lyric_line_hits_with_real_snippet(data_dir: Path) -> None:
    """[if] query matches indexed lyric exactly [then] hit returns real line snippet, [else stop]."""
    _write(data_dir, "track-a", "floating in the moonlight tonight")
    index_batch(data_dir, max_docs=10)
    hits, total = search_documents(data_dir, "moonlight", limit=10)
    assert hits == ["track-a"] and total == 1
    assert matched_snippet(data_dir, "track-a", "moonlight") == "floating in the moonlight tonight"


@pytest.mark.requirement("SEARCH-NAV-11966")
def test_fuzzy_misspelling_above_threshold_still_hits(data_dir: Path) -> None:
    """[if] query is a threshold misspelling of indexed lyrics [then] same track hits, [else stop]."""
    _write(data_dir, "track-a", "floating in the moonlight tonight")
    index_batch(data_dir, max_docs=10)
    assert lyric_text_matches_fuzzy("moonlght", "floating in the moonlight tonight")
    hits, total = search_documents(data_dir, "moonlght", limit=10)
    assert hits == ["track-a"] and total == 1
    assert matched_snippet(data_dir, "track-a", "moonlght") == "floating in the moonlight tonight"


@pytest.mark.requirement("SEARCH-NAV-11966")
def test_fuzzy_below_threshold_returns_no_hit(data_dir: Path) -> None:
    """[if] query is below fuzzy threshold [then] no lyric hit is returned, [else stop]."""
    _write(data_dir, "track-a", "floating in the moonlight tonight")
    index_batch(data_dir, max_docs=10)
    assert not lyric_text_matches_fuzzy("zzzzqqqq", "floating in the moonlight tonight")
    assert search_documents(data_dir, "zzzzqqqq", limit=10) == ([], 0)


@pytest.mark.requirement("SEARCH-NAV-11966")
def test_unindexed_cached_track_is_omitted_fail_closed(data_dir: Path) -> None:
    """[if] track has cache but no index row [then] it is omitted from hits, [else stop]."""
    _write(data_dir, "only-cache", "secret chorus line never indexed")
    assert search_documents(data_dir, "chorus", limit=10) == ([], 0)


@pytest.mark.requirement("SEARCH-NAV-11966")
def test_missing_index_file_returns_honest_empty(data_dir: Path) -> None:
    """[if] lyrics index file is missing [then] search returns empty, [else stop]."""
    assert search_documents(data_dir, "anything", limit=10) == ([], 0)


def test_documented_similarity_threshold_is_pinned() -> None:
    """[if] fuzzy threshold is consulted [then] it stays at the documented 0.72 floor, [else stop]."""
    assert LYRIC_FUZZY_SIMILARITY_THRESHOLD == 0.72
    line = best_lyric_line(
        Lyrics("x", "lrclib", (LyricLine(0, "dancing in the moonlight"),)),
        "moonlght",
    )
    assert line is not None
    assert "moonlight" in line.text.lower()


def _plain_index(rows: list[tuple[str, str]]) -> sqlite3.Connection:
    """In-memory stand-in for the ``lyrics_fts`` table with the two columns fuzzy search reads."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE lyrics_fts (stable_id TEXT, searchable_text TEXT)")
    conn.executemany("INSERT INTO lyrics_fts VALUES (?, ?)", rows)
    return conn


@pytest.mark.requirement("SEARCH-NAV-12002")
def test_fuzzy_index_pass_scores_each_distinct_word_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] many index rows repeat one vocabulary [then] scoring is bounded by vocabulary not corpus, [else stop]."""
    vocabulary = [f"word{index:02d}" for index in range(20)] + ["moonlight", "tonight", "dancing"]
    rows = [
        (f"track-{doc}", " ".join(vocabulary[(doc + k) % len(vocabulary)] for k in range(400))) for doc in range(300)
    ]
    calls = 0
    real_matcher = fuzzy_match.SequenceMatcher

    def counting_matcher(*args: object, **kwargs: object) -> SequenceMatcher:
        """Count every SequenceMatcher built by the fuzzy pass."""
        nonlocal calls
        calls += 1
        return real_matcher(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(fuzzy_match, "SequenceMatcher", counting_matcher)
    conn = _plain_index(rows)
    try:
        hits, total = merge_fts_and_fuzzy(conn, "zzzzqqqq moonlght", [], 0, limit=10)
    finally:
        conn.close()
    assert (hits, total) == ([], 0)
    assert calls <= len(vocabulary) * 2


@pytest.mark.requirement("SEARCH-NAV-12002")
def test_memoised_fuzzy_pass_agrees_with_per_document_matcher() -> None:
    """[if] the memoised index pass runs [then] it returns exactly the per-document matcher hits and total, [else stop]."""
    texts = [
        "floating in the moonlight tonight",
        "dancing in the moonlite",
        "nothing to see here",
        "moon light tonite",
        "the moonlight is gone tonight",
        "",
    ]
    rows = [(f"track-{index}", text) for index, text in enumerate(texts)]
    for query in ("moonlght", "moonlight tonight", "moon tonite", "zzzzqqqq", "dancing moonlight"):
        expected = [stable_id for stable_id, text in rows if lyric_text_matches_fuzzy(query, text)]
        conn = _plain_index(rows)
        try:
            hits, total = merge_fts_and_fuzzy(conn, query, [], 0, limit=2)
        finally:
            conn.close()
        assert hits == expected[:2], query
        assert total == len(expected), query
