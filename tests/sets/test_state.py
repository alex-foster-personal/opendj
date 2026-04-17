"""Tests for :mod:`apps.sets.state`."""
from __future__ import annotations

import sqlite3

import pytest

from apps.sets.state import Event, SetsState


@pytest.mark.requirement("SET-01")
def test_open_session_defaults_to_private(sets_state: SetsState):
    row = sets_state.open_session(
        "2026-04-17T21-30-00",
        capture_device="BlackHole 2ch",
    )
    assert row.share_state == "private"
    persisted = sets_state.get_session(row.session_id)
    assert persisted is not None
    assert persisted.share_state == "private"
    assert persisted.ended_at is None


@pytest.mark.requirement("SET-01")
def test_sets_share_state_rejects_unknown_value(sets_state: SetsState):
    """CHECK constraint guards against unknown share_state values."""
    row = sets_state.open_session("2026-04-17T21-30-00", capture_device="BlackHole 2ch")
    with pytest.raises(sqlite3.IntegrityError):
        with sets_state._rw() as conn:
            conn.execute(
                "UPDATE sets SET share_state = 'broadcast' WHERE session_id = ?",
                (row.session_id,),
            )


@pytest.mark.requirement("SET-01")
def test_record_event_roundtrip(sets_state: SetsState):
    sets_state.open_session("2026-04-17T21-30-00", capture_device="BlackHole 2ch")
    event = Event(
        session_id="2026-04-17T21-30-00",
        timestamp_s=12.5,
        wall_clock="2026-04-17T21:30:12.500+00:00",
        deck="A",
        track_stable_id="sha1:abc",
        action="track_loaded",
        source="djay_monitor",
        value={"title": "Track 1", "artist": "Artist", "duration_s": 210.0},
    )
    row_id = sets_state.record_event(event)
    assert row_id > 0
    fetched = sets_state.fetch_events("2026-04-17T21-30-00")
    assert len(fetched) == 1
    got = fetched[0]
    assert got.track_stable_id == "sha1:abc"
    assert got.deck == "A"
    assert got.action == "track_loaded"
    assert got.value["title"] == "Track 1"
    assert got.source == "djay_monitor"


@pytest.mark.requirement("SET-01")
def test_fetch_events_filters_by_action_and_since(sets_state: SetsState):
    sets_state.open_session("s1", capture_device="BlackHole 2ch")
    for i in range(3):
        sets_state.record_event(
            Event(
                session_id="s1",
                timestamp_s=float(i),
                wall_clock=f"2026-04-17T21:30:0{i}+00:00",
                action="track_loaded" if i != 1 else "heartbeat",
                source="djay_monitor",
            )
        )
    loaded = sets_state.fetch_events("s1", action="track_loaded")
    assert [e.timestamp_s for e in loaded] == [0.0, 2.0]
    later = sets_state.fetch_events("s1", since_s=1.5)
    assert [e.action for e in later] == ["track_loaded"]


@pytest.mark.requirement("SET-01")
def test_end_session_stamps_ended_at(sets_state: SetsState):
    sets_state.open_session("s1", capture_device="BlackHole 2ch")
    sets_state.end_session("s1", ended_at="2026-04-17T23:45:00.000+00:00")
    row = sets_state.get_session("s1")
    assert row is not None
    assert row.ended_at == "2026-04-17T23:45:00.000+00:00"


@pytest.mark.requirement("SET-01")
def test_count_events_tracks_writes(sets_state: SetsState):
    sets_state.open_session("s1", capture_device="BlackHole 2ch")
    assert sets_state.count_events("s1") == 0
    sets_state.record_event(
        Event(
            session_id="s1",
            timestamp_s=0.0,
            wall_clock="2026-04-17T21:30:00+00:00",
            action="session_start",
            source="recorder",
        )
    )
    assert sets_state.count_events("s1") == 1


@pytest.mark.requirement("SET-01")
def test_list_sessions_orders_by_started_at_desc(sets_state: SetsState):
    sets_state.open_session(
        "2026-04-17T21-30-00",
        capture_device="BlackHole 2ch",
        started_at="2026-04-17T21:30:00.000+00:00",
    )
    sets_state.open_session(
        "2026-04-17T22-00-00",
        capture_device="BlackHole 2ch",
        started_at="2026-04-17T22:00:00.000+00:00",
    )
    rows = sets_state.list_sessions()
    assert [r.session_id for r in rows] == [
        "2026-04-17T22-00-00",
        "2026-04-17T21-30-00",
    ]


# ---------------------------------------------------------------------------
# Phase 5 backend hook (Option A from the wire-up brief)
# ---------------------------------------------------------------------------


def test_phase5_backend_uses_shared_state_db_and_aliased_table(tmp_path):
    """SetsState(backend=apps.shared.state.db.open_rw) runs on state.db."""
    from apps.shared.state import db as state_db
    from apps.sets.state import Event, SetsState

    db = tmp_path / "state.db"
    ss = SetsState(db, backend=state_db.open_rw)
    ss.open_session("sess-1", capture_device="rekordbox")
    ss.record_event(
        Event(
            session_id="sess-1",
            timestamp_s=0.0,
            wall_clock="2026-04-17T22:00:00",
            action="LOAD",
            source="rekordbox",
            deck="A",
            track_stable_id="t1",
        )
    )

    # state.db should contain:
    #  * the Phase 5 core tables (tracks, events, adapters, ...)
    #  * the sets table
    #  * the aliased set_events (NOT our own events table)
    with sqlite3.connect(str(db)) as conn:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "tracks" in tables  # Phase 5 schema applied
        assert "sets" in tables
        assert "set_events" in tables
        # Phase 5's canonical events table with (ts, kind, ...) columns
        # is the one that owns the 'events' name.
        cols = [
            r[1]
            for r in conn.execute("PRAGMA table_info(events)").fetchall()
        ]
        assert "kind" in cols and "payload_json" in cols

        # The set_events row we just wrote is readable.
        rows = conn.execute(
            "SELECT action, source FROM set_events WHERE session_id = ?",
            ("sess-1",),
        ).fetchall()
    assert rows == [("LOAD", "rekordbox")]


def test_default_backend_still_uses_sets_db(tmp_path):
    """Without backend=, SetsState keeps its own data/sets/sets.db schema."""
    from apps.sets.state import Event, SetsState

    db = tmp_path / "sets.db"
    ss = SetsState(db)
    ss.open_session("sess-2", capture_device="rekordbox")
    ss.record_event(
        Event(
            session_id="sess-2",
            timestamp_s=0.0,
            wall_clock="2026-04-17T22:00:00",
            action="LOAD",
            source="rekordbox",
        )
    )

    with sqlite3.connect(str(db)) as conn:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert tables == {"sets", "events", "sqlite_sequence"} or (
            tables >= {"sets", "events"}
        )
        # No Phase 5 core tables under the default backend.
        assert "tracks" not in tables
