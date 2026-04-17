"""SetGoal validation + JSON round-trip (PLAY-02)."""
from __future__ import annotations

import pytest

from apps.dj_copilot.set_goal import (
    SetGoal,
    set_goal_from_dict,
    set_goal_from_json,
    set_goal_to_dict,
    set_goal_to_json,
)

pytestmark = pytest.mark.requirement("PLAY-02")


def test_happy_defaults() -> None:
    g = SetGoal(duration_min=60)
    assert g.duration_min == 60
    assert g.floor_energy == 3
    assert g.ceiling_energy == 9
    assert g.peak_at_min is None


@pytest.mark.parametrize("duration", [29, 241, -1, 0])
def test_duration_out_of_range(duration: int) -> None:
    with pytest.raises(ValueError):
        SetGoal(duration_min=duration)


def test_peak_outside_duration() -> None:
    with pytest.raises(ValueError):
        SetGoal(duration_min=60, peak_at_min=90)


def test_peak_negative() -> None:
    with pytest.raises(ValueError):
        SetGoal(duration_min=60, peak_at_min=-1)


def test_floor_above_ceiling() -> None:
    with pytest.raises(ValueError):
        SetGoal(duration_min=60, floor_energy=9, ceiling_energy=3)


def test_energy_out_of_range() -> None:
    with pytest.raises(ValueError):
        SetGoal(duration_min=60, floor_energy=0)
    with pytest.raises(ValueError):
        SetGoal(duration_min=60, ceiling_energy=11)


def test_close_on_energy_out_of_range() -> None:
    with pytest.raises(ValueError):
        SetGoal(duration_min=60, close_on_energy=11)


def test_dict_round_trip() -> None:
    g = SetGoal(
        duration_min=90,
        peak_at_min=45,
        floor_energy=3,
        ceiling_energy=9,
        open_on_key="8A",
        close_on_energy=5,
    )
    d = set_goal_to_dict(g)
    assert set_goal_from_dict(d) == g


def test_json_round_trip_is_sorted() -> None:
    g = SetGoal(duration_min=60)
    s = set_goal_to_json(g)
    assert s.index("ceiling_energy") < s.index("duration_min")
    assert set_goal_from_json(s) == g
