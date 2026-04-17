"""Tests for apps.spotify.acquisition."""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.spotify.acquisition import (
    SOURCE_TEMPLATES,
    build_acquisition_entries,
    format_duration,
    render_markdown,
    render_markdown_file,
)
from apps.spotify.client import SpotifyPlaylist, SpotifyTrack
from apps.spotify.matcher_adapter import MatchedPair


def _plist(pid="pl123", name="Pop 2026", snap="snap-1") -> SpotifyPlaylist:
    return SpotifyPlaylist(
        id=pid, name=name, snapshot_id=snap, owner="dev3", description="", tracks=(),
    )


def _pair(*, title="Mystery", artist="Who", confidence=0.0, status="unmatched",
          isrc=None, uri="spotify:track:abc", duration_ms=200000, album="Alb"):
    src = SpotifyTrack(
        spotify_id="abc", spotify_uri=uri, isrc=isrc, title=title,
        artists=(artist,), album=album, duration_ms=duration_ms, is_local=False,
    )
    return MatchedPair(source=src, target=None, confidence=confidence,
                       signals=(), status=status)


@pytest.mark.requirement("CAT-01")
def test_format_duration() -> None:
    assert format_duration(0) == "0:00"
    assert format_duration(59000) == "0:59"
    assert format_duration(60000) == "1:00"
    assert format_duration(185000) == "3:05"


@pytest.mark.requirement("CAT-01")
def test_build_entries_excludes_matched() -> None:
    pairs = [
        _pair(title="Matched", status="matched"),
        _pair(title="Review", status="review", confidence=0.55),
        _pair(title="Unmatched", status="unmatched", confidence=0.0),
    ]
    entries = build_acquisition_entries(pairs)
    assert [e.title for e in entries] == ["Unmatched", "Review"]


@pytest.mark.requirement("CAT-01")
def test_sort_by_confidence_then_title() -> None:
    pairs = [
        _pair(title="Beta", confidence=0.4, status="review"),
        _pair(title="Alpha", confidence=0.4, status="review"),
        _pair(title="Zeta", confidence=0.1, status="unmatched"),
    ]
    entries = build_acquisition_entries(pairs)
    assert [e.title for e in entries] == ["Zeta", "Alpha", "Beta"]


@pytest.mark.requirement("CAT-01")
def test_render_markdown_has_all_source_links() -> None:
    playlist = _plist()
    entries = build_acquisition_entries([_pair(title="Hello", artist="Band")])
    md = render_markdown(playlist, entries, include_timestamp=False)
    for display_name, _, _ in SOURCE_TEMPLATES:
        assert f"[{display_name}]" in md
    assert "Band" in md
    assert "Hello" in md
    assert "- [ ] purchased" in md


@pytest.mark.requirement("CAT-01")
def test_render_markdown_urlencodes() -> None:
    playlist = _plist()
    entries = build_acquisition_entries([_pair(title="Song & Dance", artist="A + B")])
    md = render_markdown(playlist, entries, include_timestamp=False)
    assert "%26" in md
    assert "%2B" in md


@pytest.mark.requirement("CAT-01")
def test_render_markdown_idempotent() -> None:
    playlist = _plist()
    entries = build_acquisition_entries([_pair(title="Hello")])
    a = render_markdown(playlist, entries, include_timestamp=False)
    b = render_markdown(playlist, entries, include_timestamp=False)
    assert a == b


@pytest.mark.requirement("CAT-01")
def test_render_markdown_empty_queue() -> None:
    playlist = _plist()
    entries = build_acquisition_entries([_pair(status="matched")])
    md = render_markdown(playlist, entries, include_timestamp=False)
    assert "Nothing to acquire" in md


@pytest.mark.requirement("CAT-01")
def test_render_markdown_file(tmp_path: Path) -> None:
    playlist = _plist()
    entries = build_acquisition_entries([_pair(title="X")])
    out = tmp_path / "to-acquire.md"
    render_markdown_file(playlist, entries, out, include_timestamp=False)
    assert out.read_text().startswith("# Acquisition queue")
