"""SetGoal pin field validation + JSON round-trip (SET-04)."""
from __future__ import annotations

import pytest

from apps.dj_copilot.set_goal import (
    SetGoal,
    set_goal_from_dict,
    set_goal_from_json,
    set_goal_to_json,
)

pytestmark = pytest.mark.requirement("SET-04")


def test_empty_pins_default_from_dict() -> None:
    g = set_goal_from_dict({"duration_min": 60})
    assert g == SetGoal(duration_min=60)
    assert g.peak_pins == ()
    assert g.opener_pins == ()
    assert g.closer_pin is None


def test_json_round_trip_preserves_pins() -> None:
    g = SetGoal(
        duration_min=60,
        peak_at_min=30,
        peak_pins=("track-a", "track-b"),
        opener_pins=("open-1", "open-2"),
        closer_pin="close-y",
    )
    restored = set_goal_from_json(set_goal_to_json(g))
    assert restored == g


def test_path_shaped_pin_raises() -> None:
    for bad_pin in ("/Music/a.mp3", "C:\\x.mp3", ".hidden"):
        with pytest.raises(ValueError, match="stable_id"):
            SetGoal(duration_min=60, peak_pins=(bad_pin,))


def test_dual_role_id_raises() -> None:
    with pytest.raises(ValueError, match="at most one pin role"):
        SetGoal(
            duration_min=60,
            peak_pins=("same-id",),
            opener_pins=("same-id",),
        )


def test_duplicate_peak_pin_raises() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        SetGoal(duration_min=60, peak_pins=("a", "a"))
