"""Tests for apps.shared.m3u8."""

from __future__ import annotations

import pytest

from apps.shared.m3u8 import M3u8Entry, m3u8_lines, render_m3u8


@pytest.mark.requirement("SET-12124")
def test_m3u8_lines_and_render() -> None:
    """[if] entries are rendered [then] EXTINF lines match the USB contract, [else stop]."""
    entries = [
        M3u8Entry(duration_s=240, artist="Alice", title="One", location="../a/one.mp3"),
        M3u8Entry(duration_s=0, artist="A\nB", title="T\nT", location="/music/t.mp3"),
    ]
    lines = m3u8_lines(entries)
    assert lines[0] == "#EXTM3U"
    assert lines[1] == "#EXTINF:240,Alice - One"
    assert lines[2] == "../a/one.mp3"
    assert lines[3] == "#EXTINF:0,A B - T T"
    body = render_m3u8(entries)
    assert body.endswith("\n")
    assert body.count("#EXTINF") == 2
