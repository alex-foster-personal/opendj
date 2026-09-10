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
    # Key round 0's scored table is still being continued in-place under
    # `### Key lane round 0`; floor stays 0 until that measurement lands so
    # a later `--post` cannot skip it.
    assert lanes.get_lane("key").round_floor == 0


def test_key_registers_krumhansl_and_skey() -> None:
    names = [c.name for c in lanes.get_lane("key").candidates]
    assert names == [
        "constant_key", "most_common_key", "truth_echo", "truth_echo_mik",
        "krumhansl", "skey",
    ]
    assert lanes.get_lane("key").fixture_builder == "scripts/build_key_bundle.py"


def test_loudness_and_waveform_lanes_have_scorers() -> None:
    """Both remaining v1 lanes now point at a module that implements the contract."""
    from apps.analysis_bench.scorers import get_scorer

    for name in ("loudness", "waveform"):
        lane = lanes.get_lane(name)
        assert lane.scorer_module is not None
        module_path = lanes.require_scorer(lane)
        assert module_path == lane.scorer_module
        module = get_scorer(module_path)
        assert hasattr(module, "SCORER_VERSION")
        assert hasattr(module, "score_bundle")
        assert hasattr(module, "render_table")


def test_an_unknown_candidate_names_the_known_ones() -> None:
    lane = lanes.get_lane("beatgrid")
    with pytest.raises(lanes.LaneError) as excinfo:
        lanes.get_candidate(lane, "madmom")
    assert "constant_128" in str(excinfo.value)
