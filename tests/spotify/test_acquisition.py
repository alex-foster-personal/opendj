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

from tests.spotify.builders import make_matched_pair, make_playlist


@pytest.mark.requirement("CAT-01")
def test_format_duration() -> None:
    assert format_duration(0) == "0:00"
    assert format_duration(59000) == "0:59"
    assert format_duration(60000) == "1:00"
    assert format_duration(185000) == "3:05"


@pytest.mark.requirement("CAT-01")
def test_build_entries_excludes_matched() -> None:
    pairs = [
        make_matched_pair(title="Matched", status="matched"),
        make_matched_pair(title="Review", status="review", confidence=0.55),
        make_matched_pair(title="Unmatched", status="unmatched", confidence=0.0),
    ]
    entries = build_acquisition_entries(pairs)
    assert [e.title for e in entries] == ["Unmatched", "Review"]


@pytest.mark.requirement("CAT-01")
def test_sort_by_confidence_then_title() -> None:
    pairs = [
        make_matched_pair(title="Beta", confidence=0.4, status="review"),
        make_matched_pair(title="Alpha", confidence=0.4, status="review"),
        make_matched_pair(title="Zeta", confidence=0.1, status="unmatched"),
    ]
    entries = build_acquisition_entries(pairs)
    assert [e.title for e in entries] == ["Zeta", "Alpha", "Beta"]


@pytest.mark.requirement("CAT-01")
def test_render_markdown_has_all_source_links() -> None:
    playlist = make_playlist()
    entries = build_acquisition_entries([make_matched_pair(title="Invented Title", artist="Band")])
    md = render_markdown(playlist, entries, include_timestamp=False)
    for display_name, _, _ in SOURCE_TEMPLATES:
        assert f"[{display_name}]" in md
    assert "Band" in md
    assert "Invented Title" in md
    assert "- [ ] purchased" in md


@pytest.mark.requirement("CAT-01")
def test_render_markdown_urlencodes() -> None:
    playlist = make_playlist()
    entries = build_acquisition_entries([make_matched_pair(title="Song & Dance", artist="A + B")])
    md = render_markdown(playlist, entries, include_timestamp=False)
    assert "%26" in md
    assert "%2B" in md


@pytest.mark.requirement("CAT-01")
def test_render_markdown_idempotent() -> None:
    playlist = make_playlist()
    entries = build_acquisition_entries([make_matched_pair(title="Invented Title")])
    a = render_markdown(playlist, entries, include_timestamp=False)
    b = render_markdown(playlist, entries, include_timestamp=False)
    assert a == b


@pytest.mark.requirement("CAT-01")
def test_render_markdown_empty_queue() -> None:
    playlist = make_playlist()
    entries = build_acquisition_entries([make_matched_pair(status="matched")])
    md = render_markdown(playlist, entries, include_timestamp=False)
    assert "Nothing to acquire" in md


@pytest.mark.requirement("CAT-01")
def test_render_markdown_file(tmp_path: Path) -> None:
    playlist = make_playlist()
    entries = build_acquisition_entries([make_matched_pair(title="X")])
    out = tmp_path / "to-acquire.md"
    render_markdown_file(playlist, entries, out, include_timestamp=False)
    assert out.read_text().startswith("# Acquisition queue")


@pytest.mark.requirement("CAT-01")
def test_render_markdown_links_track_uri() -> None:
    """[if] a track URI has an id [then] the sheet links the web track URL, [else stop]."""
    playlist = make_playlist()
    entries = build_acquisition_entries(
        [make_matched_pair(spotify_uri="spotify:track:abc", spotify_id="abc")]
    )
    md = render_markdown(playlist, entries, include_timestamp=False)
    assert "[spotify:track:abc](https://open.spotify.com/track/abc)" in md


@pytest.mark.requirement("CAT-01")
def test_render_markdown_keeps_uri_without_track_id() -> None:
    """[if] a track URI has no id [then] the sheet keeps the raw URI, [else stop]."""
    playlist = make_playlist()
    entries = build_acquisition_entries([make_matched_pair(spotify_uri="spotify:track:")])
    md = render_markdown(playlist, entries, include_timestamp=False)
    assert "[spotify:track:](spotify:track:)" in md


@pytest.mark.requirement("CAT-01")
def test_render_markdown_keeps_non_track_uri() -> None:
    """[if] the Spotify URI is not a track URI [then] the sheet keeps it raw, [else stop]."""
    playlist = make_playlist()
    entries = build_acquisition_entries(
        [make_matched_pair(spotify_uri="spotify:local:a:b:c:123")]
    )
    md = render_markdown(playlist, entries, include_timestamp=False)
    assert "[spotify:local:a:b:c:123](spotify:local:a:b:c:123)" in md
