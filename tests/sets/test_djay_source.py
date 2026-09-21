"""Tests for the djay deck-state source."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.sets.sources.djay_source import DjaySource
from apps.sets.state import SetsState


@pytest.mark.requirement("SET-01")
def test_djay_source_emits_track_loaded_and_track_change(
    sets_state: SetsState,
    djay_three_tracks_two_decks: Path,
):
    session_id = "2026-04-17T21-30-00"
    sets_state.open_session(session_id, capture_device="BlackHole 2ch")
    source = DjaySource(
        session_id=session_id,
        state=sets_state,
        db_path=djay_three_tracks_two_decks,
        session_started_at=datetime.now(UTC),
    )
    # DjayNowPlaying only reports the *latest* item per poll. Call
    # poll_once three times after injecting the latest uuid each time.
    # Since the fixture contains 3 items all in one session, monitor
    # returns the same "latest" each poll; track-change requires the
    # underlying DB to advance between polls. We simulate that by
    # swapping the monitor's last-uuid cursor then poll.
    for uuid_target in ("uuid-1", "uuid-2", "uuid-3"):
        source._monitor._last_uuid = None  # force re-emit
        # Shrink the DB to only include items up to this uuid.
        import sqlite3
        conn = sqlite3.connect(str(djay_three_tracks_two_decks))
        try:
            # Delete items after the target uuid; we rebuild per poll.
            pass  # The default fixture already contains all 3; we re-emit sequentially.
        finally:
            conn.close()
        # We use the _handle_track directly to deterministically step
        # through items; poll_once would emit only the latest row.
        items = _sorted_items(djay_three_tracks_two_decks)
        matching = next(i for i in items if i.uuid == uuid_target)
        source._handle_track(matching)

    loaded = sets_state.fetch_events(session_id, action="track_loaded")
    changed = sets_state.fetch_events(session_id, action="track_change")
    assert [e.track_stable_id for e in loaded] == ["uuid-1", "uuid-2", "uuid-3"]
    # Deck pattern A -> B -> A yields two track_change events.
    assert len(changed) == 2
    assert changed[0].value["from_deck"] == "A"
    assert changed[0].value["to_deck"] == "B"
    assert changed[1].value["from_deck"] == "B"
    assert changed[1].value["to_deck"] == "A"


@pytest.mark.requirement("SET-01")
def test_djay_source_poll_once_emits_latest_from_fixture(
    sets_state: SetsState,
    djay_three_tracks_two_decks: Path,
):
    """End-to-end poll_once against a fixture DB."""
    session_id = "s-poll"
    sets_state.open_session(session_id, capture_device="BlackHole 2ch")
    source = DjaySource(
        session_id=session_id,
        state=sets_state,
        db_path=djay_three_tracks_two_decks,
        session_started_at=datetime.now(UTC),
    )
    source.poll_once()
    loaded = sets_state.fetch_events(session_id, action="track_loaded")
    assert loaded  # at least one event; poll returns the latest history item
    assert loaded[0].source == "djay_monitor"


@pytest.mark.requirement("SET-01")
def test_djay_source_emits_source_error_when_db_missing(
    sets_state: SetsState,
    tmp_path: Path,
):
    session_id = "s-err"
    sets_state.open_session(session_id, capture_device="BlackHole 2ch")
    source = DjaySource(
        session_id=session_id,
        state=sets_state,
        db_path=tmp_path / "does-not-exist.db",
        session_started_at=datetime.now(UTC),
    )
    source.poll_once()
    errors = sets_state.fetch_events(session_id, action="source_error")
    assert len(errors) == 1
    assert errors[0].value["kind"] == "FileNotFoundError"


def _sorted_items(path: Path):
    from apps.sync.djay_monitor import get_latest_session_items

    return get_latest_session_items(path)
