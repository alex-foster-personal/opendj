"""The lane registry: what `--lane` is allowed to mean, and what it refuses."""

from __future__ import annotations

import pytest

from apps.analysis_bench import lanes


def test_the_four_v1_lanes_are_registered() -> None:
    assert set(lanes.LANES) == {"beatgrid", "key", "waveform", "loudness"}


def test_an_unknown_lane_names_the_known_ones() -> None:
    with pytest.raises(lanes.LaneError) as excinfo:
        lanes.get_lane("tempo")
    assert "beatgrid" in str(excinfo.value)


def test_every_lane_declares_both_controls() -> None:
    """A lane that cannot name its controls cannot post a round (spec section 6)."""
    for name, lane in lanes.LANES.items():
        roles = {candidate.role for candidate in lane.candidates}
        assert "positive_control" in roles, name
        assert "negative_control" in roles, name


def test_beatgrid_continues_its_own_counter() -> None:
    assert lanes.get_lane("beatgrid").round_floor == 2
    assert lanes.get_lane("key").round_floor == 0


def test_a_lane_without_a_scorer_yet_refuses_by_name() -> None:
    """Fail fast: an unbuilt lane must not score, and must say which issue owns it."""
    for name in ("loudness",):
        lane = lanes.get_lane(name)
        assert lane.scorer_module is None
        with pytest.raises(lanes.LaneError) as excinfo:
            lanes.require_scorer(lane)
        assert "#1477" in str(excinfo.value)


def test_an_unknown_candidate_names_the_known_ones() -> None:
    lane = lanes.get_lane("beatgrid")
    with pytest.raises(lanes.LaneError) as excinfo:
        lanes.get_candidate(lane, "madmom")
    assert "constant_128" in str(excinfo.value)
