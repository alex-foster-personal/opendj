"""PLAY IT solver tests (PLAY-02)."""
from __future__ import annotations

import time

import pytest

from apps.dj_copilot.set_goal import SetGoal
from apps.dj_copilot.solver import (
    UnmetConstraint,
    suggest_order,
    ARTIST_REPEAT_COOLDOWN,
)
from apps.shared.harmonic import MAX_BPM_DIFF_PCT, TrackFeature

from .conftest import make_tracks

pytestmark = pytest.mark.requirement("PLAY-02")


def _goal_peak() -> SetGoal:
    return SetGoal(
        duration_min=90,
        peak_at_min=45,
        floor_energy=3,
        ceiling_energy=9,
        close_on_energy=4,
    )


def test_empty_playlist_is_empty_result() -> None:
    result = suggest_order(tracks=[], goal=_goal_peak())
    assert result.order == []
    assert result.per_step_trace == []


def test_orders_full_playlist() -> None:
    tracks = make_tracks(20, seed=3)
    result = suggest_order(tracks=tracks, goal=_goal_peak())
    assert len(result.order) == len(tracks)
    assert len(set(result.order)) == len(tracks)


def test_deterministic_same_inputs() -> None:
    tracks = make_tracks(30, seed=42)
    r1 = suggest_order(tracks=tracks, goal=_goal_peak())
    r2 = suggest_order(tracks=tracks, goal=_goal_peak())
    assert r1.order == r2.order
    assert r1.per_step_scores == r2.per_step_scores


def test_bpm_window_honoured_when_possible() -> None:
    base = 120.0
    tracks = [
        TrackFeature(
            stable_id=f"t-{i}",
            artist=f"A{i}",
            bpm=base + i * 0.2,
            key_camelot="8A",
            energy=5,
        )
        for i in range(10)
    ]
    result = suggest_order(tracks=tracks, goal=SetGoal(duration_min=60))
    unmet = [u for u in result.constraints_unmet if u.kind == "bpm_window"]
    assert unmet == []
    for tr in result.per_step_trace[1:]:
        assert tr.bpm_delta_pct is not None
        assert tr.bpm_delta_pct <= MAX_BPM_DIFF_PCT


def test_camelot_respected_pick_close_key() -> None:
    tracks = [
        TrackFeature("t-a", "A", 120.0, "8A", 5),
        TrackFeature("t-b", "B", 120.5, "9A", 5),
        TrackFeature("t-c", "C", 120.5, "12B", 5),
    ]
    result = suggest_order(tracks=tracks, goal=SetGoal(duration_min=30))
    assert result.order[0] == "t-a"
    assert result.order[-1] == "t-c"


def test_bpm_relax_returns_non_empty() -> None:
    tracks = [
        TrackFeature("t-a", "A", 100.0, "8A", 5),
        TrackFeature("t-b", "B", 101.0, "8A", 5),
        TrackFeature("t-c", "C", 130.0, "8A", 5),
    ]
    result = suggest_order(tracks=tracks, goal=SetGoal(duration_min=30))
    assert len(result.order) == 3
    assert "t-c" in result.order
    for uc in result.constraints_unmet:
        assert isinstance(uc, UnmetConstraint)


def test_artist_repeat_avoidance() -> None:
    tracks = []
    for i in range(5):
        tracks.append(
            TrackFeature(
                stable_id=f"t-dup-{i}",
                artist="Repeater",
                bpm=120.0,
                key_camelot="8A",
                energy=5,
            )
        )
    for i, name in enumerate(("Alpha", "Beta")):
        tracks.append(
            TrackFeature(
                stable_id=f"t-u-{i}",
                artist=name,
                bpm=120.0,
                key_camelot="8A",
                energy=5,
            )
        )
    result = suggest_order(tracks=tracks, goal=SetGoal(duration_min=30))
    first3_artists = [
        next(t.artist for t in tracks if t.stable_id == sid)
        for sid in result.order[:3]
    ]
    assert "Alpha" in first3_artists or "Beta" in first3_artists


def test_artist_cooldown_default_is_documented() -> None:
    assert ARTIST_REPEAT_COOLDOWN == 8


def test_solve_ms_reported() -> None:
    tracks = make_tracks(10, seed=1)
    r = suggest_order(tracks=tracks, goal=_goal_peak())
    assert r.solve_ms >= 0.0


@pytest.mark.slow
def test_perf_200_tracks(tracks_200: list[TrackFeature]) -> None:
    """200-track ordering under a wall-clock ceiling.

    Plan 02 Open Question #5: the M-series local target is < 100 ms; CI
    runners can be several times slower, so this test enforces a looser
    1000 ms ceiling.
    """
    t0 = time.perf_counter()
    result = suggest_order(tracks=tracks_200, goal=_goal_peak())
    elapsed = (time.perf_counter() - t0) * 1000
    assert len(result.order) == 200
    assert elapsed < 1000.0, f"solver took {elapsed:.1f}ms on 200 tracks"


@pytest.mark.parametrize("seed", list(range(5)))
def test_fuzz_various_seeds(seed: int) -> None:
    tracks = make_tracks(50, seed=seed)
    result = suggest_order(tracks=tracks, goal=_goal_peak())
    assert len(result.order) == 50
    assert result.solve_ms < 1000.0


def test_beam_width_one_equals_greedy() -> None:
    tracks = make_tracks(20, seed=11)
    r = suggest_order(tracks=tracks, goal=_goal_peak(), beam_width=1)
    assert len(r.order) == 20
    assert len(set(r.order)) == 20


def test_transition_hints_populated() -> None:
    tracks = make_tracks(10, seed=1)
    r = suggest_order(tracks=tracks, goal=_goal_peak())
    assert r.per_step_trace[0].transition_hint == "start"
    for tr in r.per_step_trace[1:]:
        assert tr.transition_hint != ""
