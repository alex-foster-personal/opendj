"""PLAY IT solver tests (PLAY-02)."""

from __future__ import annotations

import os
import time

import pytest

from apps.dj_copilot.set_goal import SetGoal
from apps.dj_copilot.solver import (
    ARTIST_REPEAT_COOLDOWN,
    W_ARTIST,
    W_BPM,
    W_ENERGY,
    W_KEY,
    UnmetConstraint,
    _score_candidate_inline,
    suggest_order,
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


def test_duplicate_stable_ids_remain_distinct_playlist_occurrences() -> None:
    """A playlist membership position, not stable_id, identifies a solver input."""
    tracks = [
        TrackFeature("a", "A", 120.0, "8A", 3),
        TrackFeature("a", "A", 120.0, "8A", 4),
        TrackFeature("b", "B", 121.0, "9A", 5),
    ]

    result = suggest_order(tracks=tracks, goal=SetGoal(duration_min=30))

    assert len(result.order) == 3
    assert sorted(result.order) == ["a", "a", "b"]
    assert [trace.position for trace in result.per_step_trace] == [0, 1, 2]


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
    first3_artists = [next(t.artist for t in tracks if t.stable_id == sid) for sid in result.order[:3]]
    assert "Alpha" in first3_artists or "Beta" in first3_artists


def test_artist_cooldown_default_is_documented() -> None:
    assert ARTIST_REPEAT_COOLDOWN == 8


def test_solve_ms_reported() -> None:
    tracks = make_tracks(10, seed=1)
    r = suggest_order(tracks=tracks, goal=_goal_peak())
    assert r.solve_ms >= 0.0


@pytest.mark.slow
@pytest.mark.skipif(
    os.environ.get("CI") == "true",
    reason=(
        "Wall-clock perf test: GitHub Actions shared runners are highly "
        "variable (observed 4x+ slowdown vs local M-series). Run locally "
        "or in a dedicated perf lane; CI only guarantees correctness "
        "(len(result.order) == 200), which is covered by test_fuzz_*."
    ),
)
def test_perf_200_tracks(tracks_200: list[TrackFeature]) -> None:
    """200-track ordering under a wall-clock ceiling.

    Plan 02 Open Question #5: the M-series local target is < 100 ms; CI
    runners can be several times slower, so this test enforces a looser
    2500 ms ceiling when run locally. On GitHub Actions the test is
    skipped entirely because runner timing is not a stable signal.

    The ceiling was raised from 1000 ms -> 2500 ms after the full-suite
    post-fanout consolidation observed intermittent failures when dozens
    of parallel tests exercised the solver + audio stack concurrently;
    correctness (len(result.order) == 200) is the real invariant, and
    test_fuzz_* already covers that without wall-clock coupling.
    """
    t0 = time.perf_counter()
    result = suggest_order(tracks=tracks_200, goal=_goal_peak())
    elapsed = (time.perf_counter() - t0) * 1000
    assert len(result.order) == 200
    assert elapsed < 2500.0, f"solver took {elapsed:.1f}ms on 200 tracks"


@pytest.mark.parametrize("seed", list(range(5)))
def test_fuzz_various_seeds(seed: int) -> None:
    tracks = make_tracks(50, seed=seed)
    result = suggest_order(tracks=tracks, goal=_goal_peak())
    assert len(result.order) == 50


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


GOLDEN_COMPLETE_ORDER_SEED3 = [
    "t-00002",
    "t-00001",
    "t-00013",
    "t-00014",
    "t-00003",
    "t-00004",
    "t-00016",
    "t-00017",
    "t-00018",
    "t-00019",
    "t-00007",
    "t-00008",
    "t-00009",
    "t-00006",
    "t-00005",
    "t-00015",
    "t-00000",
    "t-00011",
    "t-00010",
    "t-00012",
]
GOLDEN_COMPLETE_SCORES_SEED3 = [
    1.0,
    0.867982,
    0.972576,
    0.907604,
    0.878884,
    0.945677,
    0.960131,
    0.839174,
    0.712392,
    0.822159,
    0.958211,
    0.675306,
    0.926802,
    0.676786,
    0.910169,
    0.633893,
    0.611862,
    0.741768,
    0.564465,
    0.573797,
]


def test_complete_fixture_keeps_current_order() -> None:
    """[if] every field is present [then] the order matches the pre-change golden, [else stop]."""
    tracks = make_tracks(20, seed=3)
    result = suggest_order(tracks=tracks, goal=_goal_peak())
    assert result.order == GOLDEN_COMPLETE_ORDER_SEED3
    assert [round(score, 6) for score in result.per_step_scores] == GOLDEN_COMPLETE_SCORES_SEED3
    assert result.unscored_edges == 0
    assert all(trace.unscored == () for trace in result.per_step_trace)


@pytest.mark.parametrize(
    ("cand", "missing"),
    [
        (TrackFeature("b", "B", 120.0, None, 5), "key"),
        (TrackFeature("b", "B", None, "8A", 5), "bpm"),
        (TrackFeature("b", "B", 120.0, "8A", None), "energy"),
    ],
    ids=["key", "bpm", "energy"],
)
def test_missing_term_is_unscored_and_weights_renormalize(cand: TrackFeature, missing: str) -> None:
    """[if] a candidate lacks one term [then] it is unscored and the rest renormalize, [else stop]."""
    prev = TrackFeature("a", "A", 120.0, "8A", 5)
    score, _relaxed, unscored = _score_candidate_inline(
        prev=prev, cand=cand, target_energy_slot=5.0, recent_artists_set=set()
    )
    assert unscored == (missing,)
    # every measurable term is perfect, so the renormalized mean is 1.0; a neutral filler would pull it below
    assert score == pytest.approx(1.0)


def test_unknown_artist_is_unscored_and_cooldown_still_applies() -> None:
    """[if] artist is unknown [then] artist is unscored; known artists still cool down, [else stop]."""
    prev = TrackFeature("a", "Known", 120.0, "8A", 5)
    unknown = TrackFeature("b", None, 120.0, "8A", 5)
    score, _relaxed, unscored = _score_candidate_inline(
        prev=prev, cand=unknown, target_energy_slot=5.0, recent_artists_set={"Known"}
    )
    assert unscored == ("artist",)
    expected = (W_KEY * 1.0 + W_BPM * 1.0 + W_ENERGY * 1.0) / (W_KEY + W_BPM + W_ENERGY)
    assert score == pytest.approx(expected)

    repeat = TrackFeature("c", "Known", 120.0, "8A", 5)
    score_repeat, _relaxed_repeat, unscored_repeat = _score_candidate_inline(
        prev=prev, cand=repeat, target_energy_slot=5.0, recent_artists_set={"Known"}
    )
    assert unscored_repeat == ()
    expected_repeat = (W_KEY + W_BPM + W_ENERGY + W_ARTIST * 0.0) / (W_KEY + W_BPM + W_ENERGY + W_ARTIST)
    assert score_repeat == pytest.approx(expected_repeat)

    other = TrackFeature("d", "Other", 120.0, "8A", 5)
    score_other, _relaxed_other, unscored_other = _score_candidate_inline(
        prev=prev, cand=other, target_energy_slot=5.0, recent_artists_set={"Known"}
    )
    assert unscored_other == ()
    assert score_other == pytest.approx(1.0)


def test_unscored_edges_count_matches_flagged_traces() -> None:
    """[if] some edges lack key, BPM or energy [then] unscored_edges matches flagged traces, [else stop]."""
    tracks = [
        TrackFeature("t-a", "A", 120.0, "8A", 5),
        TrackFeature("t-b", "B", 120.0, None, 5),
        TrackFeature("t-c", "C", None, "9A", 5),
        TrackFeature("t-d", "D", 121.0, "8A", None),
    ]
    result = suggest_order(tracks=tracks, goal=SetGoal(duration_min=30))
    flagged = [trace for trace in result.per_step_trace if trace.unscored]
    assert result.unscored_edges == len(flagged)
    assert result.unscored_edges >= 1
    assert result.per_step_trace[0].unscored == ()
    terms = {term for trace in flagged for term in trace.unscored}
    assert terms & {"key", "bpm", "energy"}
