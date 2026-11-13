"""Exact-edge tolerance tests for the GiantSteps+ boundary scorer (issue #13199).

`(private path)` section "Goal" scores a predicted
boundary as a hit inside a tolerance window around a reference boundary. These
tests lock that the window is inclusive and symmetric: a prediction exactly on
the lower edge or exactly on the upper edge is a hit, and the next float past
either edge is a miss. Both the seconds window and the 1-bar window are pinned.

-Claude
"""

from __future__ import annotations

import math

import pytest

from apps.analysis_key.giantsteps_scoring import (
    BarGrid,
    boundary_within_seconds,
    boundary_within_tolerance,
    score_seconds_track,
    score_track,
)

REFERENCE_S = 20.0
TOLERANCE_S = 3.0
LOWER_EDGE_S = REFERENCE_S - TOLERANCE_S
UPPER_EDGE_S = REFERENCE_S + TOLERANCE_S
JUST_BELOW_LOWER_S = math.nextafter(LOWER_EDGE_S, -math.inf)
JUST_ABOVE_UPPER_S = math.nextafter(UPPER_EDGE_S, math.inf)

BAR_S = 2.0  # 120 BPM, 4/4: one bar every 2 seconds.
LOWER_BAR_EDGE_S = REFERENCE_S - BAR_S
UPPER_BAR_EDGE_S = REFERENCE_S + BAR_S


def _grid(n_bars: int = 30, bar_s: float = BAR_S) -> BarGrid:
    """Constant-tempo bar grid starting at 0 s."""
    starts = tuple(i * bar_s for i in range(n_bars))
    ends = tuple((i + 1) * bar_s for i in range(n_bars))
    return BarGrid(starts=starts, ends=ends)


# -----------------------------------------------------------------------------
# seconds window
# -----------------------------------------------------------------------------


def test_seconds_prediction_exactly_on_lower_edge_is_a_hit() -> None:
    """A prediction exactly tolerance_s before the reference counts as a hit."""
    assert boundary_within_seconds(LOWER_EDGE_S, REFERENCE_S, tolerance_s=TOLERANCE_S) is True
    score = score_seconds_track([LOWER_EDGE_S], [REFERENCE_S], tolerance_s=TOLERANCE_S)
    assert (score.n_hits, score.n_misses) == (1, 0)


def test_seconds_prediction_exactly_on_upper_edge_is_a_hit() -> None:
    """A prediction exactly tolerance_s after the reference counts as a hit."""
    assert boundary_within_seconds(UPPER_EDGE_S, REFERENCE_S, tolerance_s=TOLERANCE_S) is True
    score = score_seconds_track([UPPER_EDGE_S], [REFERENCE_S], tolerance_s=TOLERANCE_S)
    assert (score.n_hits, score.n_misses) == (1, 0)


@pytest.mark.parametrize(
    "predicted_s",
    [JUST_BELOW_LOWER_S, JUST_ABOVE_UPPER_S],
    ids=["just-below-lower", "just-above-upper"],
)
def test_seconds_prediction_just_outside_either_edge_is_a_miss(predicted_s: float) -> None:
    """The next float past either edge of the seconds window is a miss."""
    assert boundary_within_seconds(predicted_s, REFERENCE_S, tolerance_s=TOLERANCE_S) is False
    score = score_seconds_track([predicted_s], [REFERENCE_S], tolerance_s=TOLERANCE_S)
    assert (score.n_hits, score.n_misses) == (0, 1)


# -----------------------------------------------------------------------------
# 1-bar window
# -----------------------------------------------------------------------------


def test_bar_prediction_exactly_one_bar_before_is_a_hit() -> None:
    """A prediction exactly one bar before the reference matches on the lower edge."""
    grid = _grid()
    assert boundary_within_tolerance(LOWER_BAR_EDGE_S, REFERENCE_S, grid) is True
    assert score_track([LOWER_BAR_EDGE_S], [REFERENCE_S], grid).n_matched == 1


def test_bar_prediction_exactly_one_bar_after_is_a_hit() -> None:
    """A prediction exactly one bar after the reference matches on the upper edge."""
    grid = _grid()
    assert boundary_within_tolerance(UPPER_BAR_EDGE_S, REFERENCE_S, grid) is True
    assert score_track([UPPER_BAR_EDGE_S], [REFERENCE_S], grid).n_matched == 1


@pytest.mark.parametrize(
    "predicted_s",
    [
        math.nextafter(LOWER_BAR_EDGE_S, -math.inf),
        math.nextafter(UPPER_BAR_EDGE_S, math.inf),
    ],
    ids=["just-below-lower", "just-above-upper"],
)
def test_bar_prediction_just_outside_either_edge_is_a_miss(predicted_s: float) -> None:
    """The next float past either 1-bar edge does not match."""
    grid = _grid()
    assert boundary_within_tolerance(predicted_s, REFERENCE_S, grid) is False
    assert score_track([predicted_s], [REFERENCE_S], grid).n_matched == 0
