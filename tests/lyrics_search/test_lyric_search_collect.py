"""Lyric hit collection for hybrid collection search (SEARCH-NAV-13563).

Regression lines:
  - if a cap above MAX_SEARCH_LIMIT is passed straight to search() and raises then broken
  - if hits beyond the first 200-row page are dropped then broken
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.lyrics.cache import LyricLine, Lyrics, cache_path, write
from apps.lyrics.search_index import MAX_SEARCH_LIMIT, index_batch
from apps.webui.server.lyric_search_collect import collect_lyric_hit_ids


def _build_index(data_dir: Path, count: int) -> None:
    """Write ``count`` real cache entries sharing one word and index them all."""
    for n in range(count):
        sid = f"track-{n:04d}"
        lyrics = Lyrics(
            stable_id=sid,
            source="lrclib",
            lines=(LyricLine(start_ms=0, text=f"moonlight serenade number {n}"),),
        )
        write(cache_path(data_dir, sid), lyrics)
    for _ in range(100):
        if index_batch(data_dir, max_docs=count).done:
            return
    raise AssertionError("index did not drain")


@pytest.mark.parametrize("cap", [MAX_SEARCH_LIMIT + 50, 400])
def test_collect_pages_past_search_limit(tmp_path: Path, cap: int) -> None:
    """A cap above MAX_SEARCH_LIMIT collects every FTS hit in batches instead of raising."""
    data_dir = tmp_path / "data"
    _build_index(data_dir, 260)

    fts_ids, _fuzzy, total = collect_lyric_hit_ids(data_dir, "moonlight", cap=cap)

    assert len(fts_ids) == min(cap, 260)
    assert total >= 260


def test_collect_with_no_matches_and_large_cap(tmp_path: Path) -> None:
    """A large cap with no lyric matches returns empty sets rather than an error."""
    data_dir = tmp_path / "data"
    _build_index(data_dir, 3)

    fts_ids, fuzzy_ids, _total = collect_lyric_hit_ids(data_dir, "zzzqqq", cap=250)

    assert fts_ids == frozenset()
    assert fuzzy_ids == frozenset()
