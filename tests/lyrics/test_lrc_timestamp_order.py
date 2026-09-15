"""LRC timestamp ordering: equal and compressed stamps parse, a real backwards jump raises.

Mon 14 Sep 2026: a lyrics library job in silver's installed app failed
track 79b8683a ("synced lyrics timestamps must be strictly increasing")
although LRCLIB (id 35886253) served valid LRC for it. The file repeats a
backing line at one stamp, twice. The fixture below keeps that response's
real timestamps and line structure; the text is replaced with placeholders
because the original is copyrighted lyrics and this repository is public.

[if] LRC timestamps repeat or never decrease [then] parsing succeeds, [else stop].
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.lyrics import cache
from apps.lyrics.service import LyricsService, Track, parse_lrc_lines

pytestmark = pytest.mark.requirement("LYRICS-01")

# Timestamps verbatim from LRCLIB 35886253, 01:45.97 to 03:08.66.
LRCLIB_35886253_SHAPE = """\
[01:45.97] line one
[01:47.92] line two
[01:49.92] line three
[01:53.97] (echo)
[01:53.97] (echo)
[01:54.04] (echo)
[01:54.06] (echo)
[01:56.09]
[02:25.22] line four
[03:04.79] line five
[03:05.23] (echo)
[03:05.38] (echo)
[03:05.38] (echo)
[03:08.66] line six
"""


class _Provider:
    def __init__(self, synced: str) -> None:
        self.synced = synced

    def fetch_synced(self, track: Track) -> str | None:
        return self.synced


def _pairs(synced: str) -> list[tuple[int, str]]:
    return [(line.start_ms, line.text) for line in parse_lrc_lines(synced)]


def test_real_lrclib_shape_with_repeated_stamps_parses() -> None:
    pairs = _pairs(LRCLIB_35886253_SHAPE)
    assert pairs.count((113970, "(echo)")) == 1, "if a verbatim repeat survives, it renders twice"
    assert pairs.count((185380, "(echo)")) == 1
    assert len(pairs) == 11, "if the blank 01:56.09 line or a real line is lost, count differs"
    assert [ms for ms, _ in pairs] == sorted(ms for ms, _ in pairs)


def test_compressed_multi_stamp_line_sorts_into_time_order() -> None:
    synced = "[00:10.00]verse\n[00:12.00][01:20.00]chorus\n[00:15.00]bridge"
    assert _pairs(synced) == [
        (10000, "verse"),
        (12000, "chorus"),
        (15000, "bridge"),
        (80000, "chorus"),
    ], "if a repeated chorus stamp stays in file order, a valid file is rejected"


def test_same_stamp_different_text_keeps_both_in_file_order() -> None:
    assert _pairs("[00:05.00]left voice\n[00:05.00]right voice") == [
        (5000, "left voice"),
        (5000, "right voice"),
    ]


def test_a_real_backwards_jump_still_raises() -> None:
    with pytest.raises(ValueError, match="jump backwards: a line stamped 10000 ms follows"):
        parse_lrc_lines("[00:20.00]later\n[00:10.00]earlier")


def test_a_compressed_line_behind_the_previous_line_still_raises() -> None:
    with pytest.raises(ValueError, match="jump backwards"):
        parse_lrc_lines("[00:30.00]late\n[00:40.00][00:20.00]out of order")


def test_equal_stamps_round_trip_through_the_cache(tmp_path: Path) -> None:
    track = Track("stable-track", "Artist", "Title", 270)
    service = LyricsService(tmp_path, _Provider("[00:05.00]left voice\n[00:05.00]right voice"))
    written = service.fetch(track)
    read_back = service.fetch(track)
    assert read_back == written, "if the cache reader rejects equal stamps, the fetch cannot reuse"


def test_the_cache_reader_still_rejects_a_decreasing_line(tmp_path: Path) -> None:
    track = Track("stable-track", "Artist", "Title", 270)
    service = LyricsService(tmp_path, _Provider("[00:05.00]first\n[00:09.00]second"))
    service.fetch(track)
    path = cache.cache_path(tmp_path, track.stable_id)
    entry = json.loads(path.read_text())
    entry["lines"][1]["start_ms"] = 1000
    path.write_text(json.dumps(entry))
    with pytest.raises(ValueError, match="invalid line 1"):
        service.fetch(track)
