"""Fuzzy lyric also-search over the committed index (issue #11145)."""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.lyrics.cache import LyricLine, Lyrics, cache_path, write
from apps.lyrics.fuzzy_match import (
    LYRIC_FUZZY_SIMILARITY_THRESHOLD,
    best_lyric_line,
    lyric_text_matches_fuzzy,
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
