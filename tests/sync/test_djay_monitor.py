"""Tests for the ported djay now-playing monitor."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.sync.djay_monitor import (
    DjayNowPlaying,
    coredata_timestamp_to_datetime,
    get_latest_session_items,
    get_now_playing,
)


@pytest.mark.requirement("SET-01")
def test_coredata_timestamp_roundtrips_to_utc_datetime():
    # 2026-01-01 00:00:00 UTC in Core Data seconds = 788918400
    dt = coredata_timestamp_to_datetime(788918400.0)
    assert dt == datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.requirement("SET-01")
def test_coredata_timestamp_none_passthrough():
    assert coredata_timestamp_to_datetime(None) is None


@pytest.mark.requirement("SET-01")
def test_get_latest_session_items_raises_on_missing_db(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        get_latest_session_items(tmp_path / "nope.db")


@pytest.mark.requirement("SET-01")
def test_get_latest_session_items_returns_parsed_rows(
    djay_three_tracks_two_decks: Path,
):
    items = get_latest_session_items(djay_three_tracks_two_decks)
    assert [i.uuid for i in items] == ["uuid-1", "uuid-2", "uuid-3"]
    assert [i.deck_number for i in items] == [1, 2, 1]
    assert all(i.session_uuid == "session-1" for i in items)
    now = get_now_playing(items)
    assert now is not None and now.uuid == "uuid-3"


@pytest.mark.requirement("SET-01")
def test_parse_history_item_recovers_strings_and_numbers(
    djay_three_tracks_two_decks: Path,
):
    items = get_latest_session_items(djay_three_tracks_two_decks)
    first = items[0]
    assert first.title == "Track 1"
    assert first.artist == "Artist 1"
    assert first.duration == 180.0


@pytest.mark.requirement("SET-01")
def test_djay_now_playing_fires_callback_only_on_new_uuid(
    djay_three_tracks_two_decks: Path,
):
    seen: list[str] = []
    monitor = DjayNowPlaying(
        on_new_track=lambda item: seen.append(item.uuid),
        db_path=djay_three_tracks_two_decks,
    )
    # Poll twice against the same DB; second poll has nothing new.
    monitor.poll()
    monitor.poll()
    assert seen == ["uuid-3"]
    assert monitor.current_track is not None
    assert monitor.current_track.uuid == "uuid-3"
