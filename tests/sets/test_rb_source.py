"""Tests for the Rekordbox HISTORY deck-state source."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.sets.sources.rb_source import RekordboxHistorySource
from apps.sets.state import SetsState


@pytest.mark.requirement("SET-01")
def test_rb_source_emits_track_loaded_per_history_row(
    sets_state: SetsState,
    rb_history_db: Path,
):
    session_id = "s-rb"
    sets_state.open_session(session_id, capture_device="BlackHole 2ch")
    source = RekordboxHistorySource(
        session_id=session_id,
        state=sets_state,
        db_path=rb_history_db,
        session_started_at=datetime.now(UTC),
    )
    source.poll_once()
    loaded = sets_state.fetch_events(session_id, action="track_loaded")
    assert [e.track_stable_id for e in loaded] == ["c-1", "c-2", "c-3"]
    assert all(e.source == "rb_history" for e in loaded)
    assert loaded[0].deck is None
    assert loaded[0].value["note"].startswith("RB HISTORY writes on track END")


@pytest.mark.requirement("SET-01")
def test_rb_source_only_emits_new_rows_on_second_poll(
    sets_state: SetsState,
    rb_history_db: Path,
):
    session_id = "s-rb-2"
    sets_state.open_session(session_id, capture_device="BlackHole 2ch")
    source = RekordboxHistorySource(
        session_id=session_id,
        state=sets_state,
        db_path=rb_history_db,
        session_started_at=datetime.now(UTC),
    )
    source.poll_once()
    # Insert a new row in the HISTORY playlist.
    conn = sqlite3.connect(str(rb_history_db))
    try:
        conn.execute(
            "INSERT INTO djmdSongPlaylist(ID, PlaylistID, ContentID, TrackNo)"
            " VALUES (?, ?, ?, ?)",
            ("sp-4", "hist-1", "c-4", 4),
        )
        conn.commit()
    finally:
        conn.close()

    source.poll_once()
    loaded = sets_state.fetch_events(session_id, action="track_loaded")
    ids = [e.track_stable_id for e in loaded]
    assert ids == ["c-1", "c-2", "c-3", "c-4"]  # one new row only


@pytest.mark.requirement("SET-01")
def test_rb_source_emits_source_error_when_db_missing(
    sets_state: SetsState,
    tmp_path: Path,
):
    session_id = "s-rb-err"
    sets_state.open_session(session_id, capture_device="BlackHole 2ch")
    source = RekordboxHistorySource(
        session_id=session_id,
        state=sets_state,
        db_path=tmp_path / "nope.db",
        session_started_at=datetime.now(UTC),
    )
    source.poll_once()
    errors = sets_state.fetch_events(session_id, action="source_error")
    assert len(errors) == 1
    assert errors[0].value["kind"] == "FileNotFoundError"


@pytest.mark.requirement("SET-01")
def test_rb_source_handles_empty_history(sets_state: SetsState, tmp_path: Path):
    from tests.sets.conftest import make_rb_history_fixture

    db = tmp_path / "master_empty.db"
    make_rb_history_fixture(db, [])  # no rows in the history
    session_id = "s-rb-empty"
    sets_state.open_session(session_id, capture_device="BlackHole 2ch")
    source = RekordboxHistorySource(
        session_id=session_id,
        state=sets_state,
        db_path=db,
        session_started_at=datetime.now(UTC),
    )
    source.poll_once()
    loaded = sets_state.fetch_events(session_id, action="track_loaded")
    assert loaded == []
