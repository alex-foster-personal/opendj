"""Matched-phrase-in-context snippets (Part 3 of #935, issue #1344).

Every case builds a REAL lyrics cache entry on disk with the production
writer (``apps.lyrics.cache.write``), same convention as
test_search_index.py, so a snippet always comes from a real cached
transcript rather than a stubbed line list.

Regression lines:
  - if the snippet picks the FIRST line containing any term instead of the
    line matching the MOST terms then broken
  - if a query with no terms returns a snippet instead of None then broken
  - if a missing/unreadable cache entry raises instead of returning None then broken
"""

from __future__ import annotations

from pathlib import Path

from apps.lyrics.cache import LyricLine, Lyrics, cache_path, write
from apps.lyrics.search_snippet import matched_snippet


def _write(data_dir: Path, stable_id: str, lines: list[tuple[int, str]]) -> None:
    write(
        cache_path(data_dir, stable_id),
        Lyrics(
            stable_id=stable_id,
            source="lrclib",
            lines=tuple(LyricLine(start_ms=ms, text=text) for ms, text in lines),
        ),
    )


def test_returns_the_line_matching_the_most_query_terms(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "s1",
        [
            (0, "walking on the sunny side"),
            (1000, "dancing in the pale moonlight"),
            (2000, "sunny days and moonlight nights"),
        ],
    )

    snippet = matched_snippet(tmp_path, "s1", "sunny moonlight")

    assert snippet == "sunny days and moonlight nights"


def test_falls_back_to_earliest_line_with_any_term_when_no_line_has_both(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "s1",
        [
            (0, "walking on the sunny side"),
            (1000, "nothing relevant here at all"),
            (2000, "dancing in the pale moonlight"),
        ],
    )

    snippet = matched_snippet(tmp_path, "s1", "sunny moonlight")

    assert snippet == "walking on the sunny side"


def test_blank_query_returns_none(tmp_path: Path) -> None:
    _write(tmp_path, "s1", [(0, "some lyric line")])

    assert matched_snippet(tmp_path, "s1", "   ") is None


def test_missing_cache_entry_returns_none_rather_than_raising(tmp_path: Path) -> None:
    assert matched_snippet(tmp_path, "never-cached", "sunny") is None
