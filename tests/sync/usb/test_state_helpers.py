"""Tests for helper functions in apps.sync.usb.state that don't need RB."""
from __future__ import annotations

import pytest

from apps.sync.usb.state import dedupe_preserving_playlists, group_by_playlist


@pytest.mark.requirement("CAT-02")
def test_group_by_playlist(fixture_canonical) -> None:
    grouped = group_by_playlist(fixture_canonical)
    assert set(grouped) == {"Warmup", "Peak"}
    assert len(grouped["Warmup"]) == 2
    assert len(grouped["Peak"]) == 1


@pytest.mark.requirement("CAT-02")
def test_dedupe_by_stable_id(fixture_canonical, make_track) -> None:
    # Add a duplicate of the first track.
    extra = make_track(
        title="DupeOne",
        artist="Alice",
        album="AA",
        playlist="Peak",
        stable_id=fixture_canonical[0].stable_id,
    )
    deduped = dedupe_preserving_playlists(fixture_canonical + [extra])
    assert len(deduped) == 3  # only original stable_ids


@pytest.mark.requirement("CAT-02")
def test_load_canonical_tracks_shared_state_path_falls_through(
    monkeypatch,
) -> None:
    """--from-shared-state currently falls through to the RB shim.

    We monkeypatch the shim to avoid needing a real RB DB, then confirm
    the call still returns via that path.
    """
    called = {"n": 0}

    def fake_rb(*, playlist_names, db=None, hash_cache=None):
        called["n"] += 1
        return []

    monkeypatch.setattr("apps.sync.usb.state.load_from_rekordbox", fake_rb)
    from apps.sync.usb.state import load_canonical_tracks

    result = load_canonical_tracks(
        playlist_names=["Any"], use_shared_state=True
    )
    assert result == []
    assert called["n"] == 1
