"""Plumbing coverage for the boundary-F@1bar scorer, on plain numbers.

Per `specs/native-analysis-v1-lanes/nav1-key-record.md`: a mutation test that
injects an offset and checks the boundary moves with it is REGRESSION coverage
of the scoring plumbing, never the real NATIVE-05 measurement (that is
`test_giantsteps_plus_boundary.py`, on real GiantSteps+ audio through the real
analyzer). This file only proves the scorer itself is not vacuously green:
a genuinely aligned boundary must match, and shifting the SAME boundary by 2
bars must turn that match into a miss.

-Claude Sonnet 5
"""

from __future__ import annotations

import pytest

from apps.analysis_key.giantsteps_scoring import (
    BarGrid,
    bar_duration_at,
    boundary_within_tolerance,
    score_track,
)

BAR_S = 2.0  # 120 BPM, 4/4: one bar every 2 seconds.


def _grid(n_bars: int, bar_s: float = BAR_S) -> BarGrid:
    starts = tuple(i * bar_s for i in range(n_bars))
    ends = tuple((i + 1) * bar_s for i in range(n_bars))
    return BarGrid(starts=starts, ends=ends)


def test_bar_duration_at_reads_the_covering_bar() -> None:
    grid = _grid(10)
    assert bar_duration_at(grid, 5.3) == pytest.approx(BAR_S)


def test_bar_duration_at_the_end_uses_the_last_bar() -> None:
    grid = _grid(10)
    assert bar_duration_at(grid, 999.0) == pytest.approx(BAR_S)


def test_bar_duration_at_before_the_grid_uses_the_first_bar() -> None:
    grid = BarGrid(starts=(0.0, 3.0, 5.0), ends=(3.0, 5.0, 7.0))
    assert bar_duration_at(grid, -1.0) == pytest.approx(3.0)


def test_a_boundary_within_one_bar_matches() -> None:
    grid = _grid(20)
    # Annotated at bar 10 (t=20.0); detected half a bar early.
    assert boundary_within_tolerance(19.0, 20.0, grid) is True


def test_a_boundary_exactly_one_bar_away_still_matches() -> None:
    grid = _grid(20)
    assert boundary_within_tolerance(18.0, 20.0, grid, tolerance_bars=1.0) is True


#-----------------------------------------------------------------------------
# the mandated mutation: shift the annotation by 2 bars and it must go red
#-----------------------------------------------------------------------------

def test_shifting_the_annotation_by_two_bars_turns_a_match_into_a_miss() -> None:
    """The scorer's own validity check: it must not be trivially green.

    A detected boundary that genuinely matches the true annotation must stop
    matching once the annotation is corrupted by 2 real bars -- if it did not,
    the "within 1 bar" check would not actually be discriminating anything.
    """
    grid = _grid(20)
    detected = 20.0
    true_annotation = 20.0
    corrupted_annotation = true_annotation + 2 * BAR_S

    assert boundary_within_tolerance(detected, true_annotation, grid) is True
    assert boundary_within_tolerance(detected, corrupted_annotation, grid) is False


def test_score_track_mutation_two_bar_shift_drops_f_measure_to_zero() -> None:
    grid = _grid(60)
    detected = [20.0, 60.0]
    true_annotations = [20.0, 60.0]
    corrupted_annotations = [a + 2 * BAR_S for a in true_annotations]

    true_score = score_track(detected, true_annotations, grid)
    assert true_score.n_matched == 2
    assert true_score.f_measure == pytest.approx(1.0)

    corrupted_score = score_track(detected, corrupted_annotations, grid)
    assert corrupted_score.n_matched == 0
    assert corrupted_score.f_measure == pytest.approx(0.0)


#-----------------------------------------------------------------------------
# precision/recall/matching shape
#-----------------------------------------------------------------------------

def test_a_false_positive_detection_costs_precision_not_recall() -> None:
    grid = _grid(60)
    score = score_track(
        detected_boundaries_s=[20.0, 45.0], annotated_boundaries_s=[20.0], grid=grid,
    )
    assert score.n_matched == 1
    assert score.recall == pytest.approx(1.0)
    assert score.precision == pytest.approx(0.5)


def test_a_missed_annotation_costs_recall_not_precision() -> None:
    grid = _grid(60)
    score = score_track(
        detected_boundaries_s=[20.0], annotated_boundaries_s=[20.0, 45.0], grid=grid,
    )
    assert score.n_matched == 1
    assert score.precision == pytest.approx(1.0)
    assert score.recall == pytest.approx(0.5)


def test_matching_is_one_to_one_not_double_counted() -> None:
    """Two annotations near one detection must not both claim it."""
    grid = _grid(60)
    score = score_track(
        detected_boundaries_s=[20.0], annotated_boundaries_s=[19.0, 21.0], grid=grid,
    )
    assert score.n_matched == 1


def test_greedy_nearest_first_would_undercount_this_maximum_matching() -> None:
    """sol-review #3948 P1 BLOCKING counter-example: annotated at 10s/12s,
    detected at 8.5s/11s, tolerance 2s. Processing annotations in input
    order and grabbing each one's NEAREST free detection gives 10<->11 (the
    nearer pair), which then starves 12 (only 8.5 is left, dist 3.5, outside
    tolerance) -- a greedy matched=1. The true maximum matching is size 2:
    10<->8.5 (dist 1.5) and 12<->11 (dist 1), both individually within
    tolerance and mutually compatible. `score_track` must find the maximum,
    not the greedy-nearest-first result."""
    grid = _grid(60, bar_s=2.0)  # tolerance_bars=1.0 * 2.0s/bar = 2s tolerance
    score = score_track(
        detected_boundaries_s=[8.5, 11.0], annotated_boundaries_s=[10.0, 12.0], grid=grid,
    )
    assert score.n_matched == 2, "a valid size-2 matching exists; greedy nearest-first finds only 1"


def test_no_annotated_and_no_detected_scores_zero_not_a_divide_by_zero() -> None:
    """A single-key track (no modulation) is out of this scorer's domain, so
    the empty case is defined as 0.0 rather than raising or faking a 1.0:
    nothing was measured, so nothing is claimed as a match."""
    grid = _grid(60)
    score = score_track(detected_boundaries_s=[], annotated_boundaries_s=[], grid=grid)
    assert (score.precision, score.recall, score.f_measure) == (0.0, 0.0, 0.0)


def test_zero_bars_in_the_grid_refuses_rather_than_dividing_by_zero() -> None:
    with pytest.raises(ValueError, match="zero bars"):
        bar_duration_at(BarGrid(starts=(), ends=()), 10.0)
