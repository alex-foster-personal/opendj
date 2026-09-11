"""PLAY IT track pinning solver tests (SET-04).

[if] a set goal carries pin roles [then] peak opener and closer pins land in their windows, [else stop].
"""
from __future__ import annotations

import pytest

from apps.dj_copilot.pinning import PinUnsatisfiableError, assign_peak_pin_slots
from apps.dj_copilot.set_goal import SetGoal
from apps.dj_copilot.solver import suggest_order

from .conftest import make_tracks

pytestmark = pytest.mark.requirement("SET-04")


def test_peak_pins_land_in_peak_window() -> None:
    tracks = make_tracks(15, seed=3)
    peak_ids = (tracks[6].stable_id, tracks[7].stable_id, tracks[8].stable_id)
    goal = SetGoal(
        duration_min=60,
        peak_at_min=30,
        peak_pins=peak_ids,
        opener_pins=(tracks[0].stable_id, tracks[1].stable_id, tracks[2].stable_id),
        closer_pin=tracks[14].stable_id,
    )
    result = suggest_order(tracks=tracks, goal=goal)
    peak_slots = set(assign_peak_pin_slots(goal, len(tracks)).keys())
    for slot, stable_id in enumerate(result.order):
        if stable_id in peak_ids:
            assert slot in peak_slots


def test_opener_and_closer_honored() -> None:
    tracks = make_tracks(15, seed=5)
    openers = (
        tracks[0].stable_id,
        tracks[1].stable_id,
        tracks[2].stable_id,
    )
    closer = tracks[14].stable_id
    goal = SetGoal(
        duration_min=60,
        peak_at_min=30,
        opener_pins=openers,
        closer_pin=closer,
    )
    result = suggest_order(tracks=tracks, goal=goal)
    assert result.order[0] in openers
    assert result.order[-1] == closer


def test_missing_peak_pin_refuses() -> None:
    tracks = make_tracks(10, seed=1)
    goal = SetGoal(duration_min=60, peak_pins=("missing-id",))
    with pytest.raises(PinUnsatisfiableError) as excinfo:
        suggest_order(tracks=tracks, goal=goal)
    assert excinfo.value.reason == "pin_not_in_playlist"
    assert "missing-id" in excinfo.value.missing


def test_empty_pins_match_unpinned_order() -> None:
    tracks = make_tracks(20, seed=11)
    goal = SetGoal(duration_min=60, peak_at_min=30)
    unpinned = suggest_order(tracks=tracks, goal=goal)
    pinned_goal = SetGoal(
        duration_min=60,
        peak_at_min=30,
        peak_pins=(),
        opener_pins=(),
        closer_pin=None,
    )
    again = suggest_order(tracks=tracks, goal=pinned_goal)
    assert again.order == unpinned.order


def test_peak_window_too_small() -> None:
    tracks = make_tracks(4, seed=2)
    goal = SetGoal(
        duration_min=30,
        peak_at_min=15,
        peak_pins=(
            tracks[0].stable_id,
            tracks[1].stable_id,
            tracks[2].stable_id,
        ),
    )
    with pytest.raises(PinUnsatisfiableError) as excinfo:
        suggest_order(tracks=tracks, goal=goal)
    assert excinfo.value.reason == "peak_window_too_small"


def test_deterministic_pinned_order() -> None:
    tracks = make_tracks(15, seed=8)
    goal = SetGoal(
        duration_min=60,
        peak_at_min=30,
        peak_pins=(tracks[6].stable_id, tracks[7].stable_id),
        opener_pins=(tracks[0].stable_id,),
        closer_pin=tracks[14].stable_id,
    )
    first = suggest_order(tracks=tracks, goal=goal)
    second = suggest_order(tracks=tracks, goal=goal)
    assert first.order == second.order
