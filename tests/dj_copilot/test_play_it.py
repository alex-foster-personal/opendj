"""play_it end-to-end tests (PLAY-02)."""
from __future__ import annotations

import json
import sqlite3

import pytest

from apps.dj_copilot.play_it import InsufficientDataError, play_it
from apps.dj_copilot.set_goal import SetGoal, set_goal_from_json
from apps.shared.harmonic import TrackFeature
from apps.shared.play_orders import load_play_order

from .conftest import make_tracks

pytestmark = pytest.mark.requirement("PLAY-02")


def test_end_to_end_persists_order(po_conn: sqlite3.Connection) -> None:
    tracks = make_tracks(15, seed=9)
    goal = SetGoal(duration_min=60, peak_at_min=30)
    po_id, result = play_it(
        conn=po_conn, playlist_id="pl-e2e", goal=goal, tracks=tracks
    )
    assert po_id > 0
    assert len(result.order) == 15

    loaded = load_play_order(po_conn, "pl-e2e", "PLAY IT")
    assert loaded.generated_by == "play-it"
    assert loaded.goal_json is not None
    assert set_goal_from_json(loaded.goal_json) == goal
    assert [e.stable_id for e in loaded.entries] == result.order


def test_pre_flight_refuses_missing_bpm(po_conn: sqlite3.Connection) -> None:
    tracks = [
        TrackFeature(f"t-{i}", "A", 120.0, "8A", 5) for i in range(5)
    ] + [TrackFeature("t-bad", "B", None, "8A", 5)]
    goal = SetGoal(duration_min=60)
    with pytest.raises(InsufficientDataError) as excinfo:
        play_it(conn=po_conn, playlist_id="pl-bad", goal=goal, tracks=tracks)
    assert "t-bad" in str(excinfo.value)
    assert "bpm" in str(excinfo.value)


def test_pre_flight_allows_small_gaps(po_conn: sqlite3.Connection) -> None:
    tracks = [
        TrackFeature(f"t-{i:03d}", "A", 120.0 + i * 0.05, "8A", 5)
        for i in range(100)
    ]
    tracks[0] = TrackFeature("t-000", "A", None, "8A", 5)
    goal = SetGoal(duration_min=60)
    po_id, _ = play_it(
        conn=po_conn, playlist_id="pl-1pct", goal=goal, tracks=tracks
    )
    assert po_id > 0


def test_overwrite_replaces_existing(po_conn: sqlite3.Connection) -> None:
    tracks = make_tracks(10, seed=4)
    goal = SetGoal(duration_min=60)
    play_it(conn=po_conn, playlist_id="pl-ow", goal=goal, tracks=tracks)
    with pytest.raises(sqlite3.IntegrityError):
        play_it(conn=po_conn, playlist_id="pl-ow", goal=goal, tracks=tracks)
    po_id, _ = play_it(
        conn=po_conn,
        playlist_id="pl-ow",
        goal=goal,
        tracks=tracks,
        overwrite=True,
    )
    assert po_id > 0


def test_custom_name(po_conn: sqlite3.Connection) -> None:
    tracks = make_tracks(8, seed=5)
    goal = SetGoal(duration_min=60)
    play_it(
        conn=po_conn,
        playlist_id="pl-named",
        goal=goal,
        tracks=tracks,
        name="peak-set",
    )
    loaded = load_play_order(po_conn, "pl-named", "peak-set")
    assert loaded.name == "peak-set"


def test_goal_json_valid_json(po_conn: sqlite3.Connection) -> None:
    tracks = make_tracks(5, seed=1)
    goal = SetGoal(duration_min=60, peak_at_min=30)
    play_it(conn=po_conn, playlist_id="pl-g", goal=goal, tracks=tracks)
    loaded = load_play_order(po_conn, "pl-g", "PLAY IT")
    json.loads(loaded.goal_json)
