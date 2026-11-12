"""[if] piecewise map scoring ships [then] coverage and phase split per #11363, [else stop]."""

from __future__ import annotations

import pytest

from apps.analysis_beatgrid.segment_score import PiecewiseMap, ValueSegment, score_piecewise_maps
from apps.analysis_key.giantsteps_scoring import (
    BarGrid,
    boundary_within_tolerance,
    max_cardinality_boundary_matches,
    score_track,
)

pytestmark = pytest.mark.requirement("SEGMENTS-12681")


def _grid(n_bars: int, *, bar_s: float = 2.0) -> BarGrid:
    starts = tuple(i * bar_s for i in range(n_bars))
    ends = tuple((i + 1) * bar_s for i in range(n_bars))
    return BarGrid(starts=starts, ends=ends)


def _variable_grid() -> BarGrid:
    starts = (0.0, 4.0, 5.0, 6.0, 7.0)
    ends = (4.0, 5.0, 6.0, 7.0, 8.0)
    return BarGrid(starts=starts, ends=ends)


def _const_map(bpm: float, duration_s: float = 100.0) -> PiecewiseMap:
    return PiecewiseMap(duration_s, (ValueSegment(0.0, duration_s, bpm),))


def _two_bpm_map(bpm_a: float, end_a: float, bpm_b: float, duration_s: float = 100.0) -> PiecewiseMap:
    return PiecewiseMap(
        duration_s,
        (
            ValueSegment(0.0, end_a, bpm_a),
            ValueSegment(end_a, duration_s, bpm_b),
        ),
    )


def test_eighty_percent_value_match_yields_coverage_without_phase_penalty():
    """[if] tempo matches on 80% of time and is wrong on 20% [then] coverage is 0.8 and phase stays high, [else stop]."""
    truth = _two_bpm_map(120.0, 80.0, 120.0)
    detected = _two_bpm_map(120.0, 80.0, 130.0)
    grid = _grid(50)
    result = score_piecewise_maps(truth, detected, grid)
    assert result.coverage == pytest.approx(0.8)
    assert result.phase == pytest.approx(1.0)
    assert result.coverage != result.phase


def test_correct_tempo_with_off_bar_beats_keeps_coverage_high_and_phase_low():
    """[if] tempo is right but beats sit off the bar line [then] coverage high and phase fails, [else stop]."""
    bpm = 120.0
    truth = _const_map(bpm)
    detected = _const_map(bpm)
    grid = _grid(50)
    bar_s = 2.0
    truth_beats = [i * bar_s for i in range(10)]
    detected_beats = [i * bar_s + bar_s * 0.5 for i in range(10)]
    result = score_piecewise_maps(
        truth,
        detected,
        grid,
        truth_beats=truth_beats,
        detected_beats=detected_beats,
    )
    assert result.coverage == pytest.approx(1.0)
    assert result.phase < 0.5
    assert result.phase < result.coverage


def test_variable_tempo_boundary_uses_bar_coordinate_distance_not_seconds():
    """[if] boundary is one bar off on variable BarGrid [then] phase uses bar coords not seconds, [else stop]."""
    grid = _variable_grid()
    truth = _two_bpm_map(120.0, 4.0, 130.0, duration_s=8.0)
    detected = _two_bpm_map(120.0, 4.0, 130.0, duration_s=8.0)
    assert score_piecewise_maps(truth, detected, grid).boundary_recall == pytest.approx(1.0)
    assert score_track([0.0], [4.0], grid).n_matched == 1
    starts = (0.0, 10.0, 11.0, 12.0, 13.0)
    ends = (10.0, 11.0, 12.0, 13.0, 14.0)
    long_grid = BarGrid(starts=starts, ends=ends)
    annotated = 9.5
    seconds_close = 12.5
    assert boundary_within_tolerance(seconds_close, annotated, long_grid) is False


def test_boundary_matching_is_max_cardinality_one_to_one():
    """[if] two truth boundaries could match one detection [then] max cardinality finds both, [else stop]."""
    grid = _grid(60, bar_s=2.0)
    matched = max_cardinality_boundary_matches(
        [10.0, 12.0],
        [8.5, 11.0],
        grid,
        tolerance_bars=1.0,
    )
    assert matched == 2
    score = score_track([8.5, 11.0], [10.0, 12.0], grid)
    assert score.n_matched == 2
