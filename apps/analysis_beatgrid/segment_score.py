"""Pure coverage vs phase scoring for piecewise tempo or key maps.

Parent issue #4480: report how much of a track carries the right value
(coverage) separately from whether change boundaries and beats sit on the bar
grid (phase). Audio-free: maps in, two floats out.

Coverage walks the union of both maps' segment edges and sums time where the
active value agrees. Phase never folds into that sum.

Phase combines two checks when the caller supplies them:
  * interior segment boundaries, matched with GiantSteps+ 1-bar bar-coordinate
    tolerance and maximum-cardinality one-to-one assignment;
  * optional beat times within value-matching regions, scored by distance to the
    nearest bar line via ``bar_phase.distance_to_bar_line_bars``.

The beat half is what keeps correct-tempo but off-grid beats from inflating
phase; the boundary half is what keeps variable-tempo grids honest.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from apps.analysis_beatgrid.bar_phase import distance_to_bar_line_bars
from apps.analysis_key.giantsteps_scoring import BarGrid, score_track

DEFAULT_ON_BAR_TOLERANCE_BARS = 0.05


def _values_equal_default(a: object, b: object) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return math.isclose(a, b, rel_tol=0.0, abs_tol=1e-6)
    return a == b


@dataclass(frozen=True)
class ValueSegment:
    """One half-open ``[start_s, end_s)`` span with a single constant value."""

    start_s: float
    end_s: float
    value: object


@dataclass(frozen=True)
class PiecewiseMap:
    """Piecewise-constant map over ``[0, duration_s)``."""

    duration_s: float
    segments: tuple[ValueSegment, ...]


@dataclass(frozen=True)
class SegmentMapScore:
    """Separate coverage and phase; neither folds into the other."""

    coverage: float
    phase: float
    boundary_recall: float
    beat_on_bar: float | None


def _validate_map(m: PiecewiseMap) -> None:
    if m.duration_s <= 0:
        raise ValueError("duration_s must be positive")
    if not m.segments:
        raise ValueError("map must have at least one segment")
    prev_end = 0.0
    for index, segment in enumerate(m.segments):
        if segment.start_s < prev_end:
            raise ValueError(f"segments overlap at index {index}")
        if segment.end_s <= segment.start_s:
            raise ValueError(f"non-positive span at index {index}")
        prev_end = segment.end_s
    if abs(prev_end - m.duration_s) > 1e-9:
        raise ValueError("segments must exactly cover duration_s")


def _value_at(m: PiecewiseMap, t: float) -> object:
    for segment in m.segments:
        if segment.start_s <= t < segment.end_s:
            return segment.value
    if abs(t - m.duration_s) < 1e-9 and m.segments:
        return m.segments[-1].value
    raise ValueError(f"time {t}s is outside map duration {m.duration_s}s")


def _breakpoints(truth: PiecewiseMap, detected: PiecewiseMap) -> list[float]:
    points = {0.0, truth.duration_s, detected.duration_s}
    for segment in (*truth.segments, *detected.segments):
        points.add(segment.start_s)
        points.add(segment.end_s)
    return sorted(points)


def _interior_boundaries(m: PiecewiseMap) -> tuple[float, ...]:
    if len(m.segments) < 2:
        return ()
    return tuple(segment.start_s for segment in m.segments[1:])


def _values_match_at_time(
    truth: PiecewiseMap,
    detected: PiecewiseMap,
    t: float,
    values_equal: Callable[[object, object], bool],
) -> bool:
    if t >= truth.duration_s or t >= detected.duration_s:
        return False
    return values_equal(_value_at(truth, t), _value_at(detected, t))


def coverage_fraction(
    truth: PiecewiseMap,
    detected: PiecewiseMap,
    *,
    values_equal: Callable[[object, object], bool] = _values_equal_default,
) -> float:
    """Share of track time where truth and detected values agree."""
    _validate_map(truth)
    _validate_map(detected)
    if abs(truth.duration_s - detected.duration_s) > 1e-9:
        raise ValueError("truth and detected maps must share duration_s")

    matched_s = 0.0
    cuts = _breakpoints(truth, detected)
    for start, end in zip(cuts, cuts[1:]):
        if end <= start:
            continue
        sample = start + (end - start) * 0.5
        if _values_match_at_time(truth, detected, sample, values_equal):
            matched_s += end - start
    return matched_s / truth.duration_s


def _boundary_phase_recall(
    truth: PiecewiseMap,
    detected: PiecewiseMap,
    grid: BarGrid,
    *,
    tolerance_bars: float,
) -> float:
    annotated = _interior_boundaries(truth)
    if not annotated:
        return 1.0
    detected_bounds = _interior_boundaries(detected)
    return score_track(detected_bounds, annotated, grid, tolerance_bars=tolerance_bars).recall


def _beat_on_bar_fraction(
    detected_beats: Sequence[float],
    grid: BarGrid,
    *,
    on_bar_tolerance_bars: float,
    predicate: Callable[[float], bool],
) -> float:
    eligible = [t for t in detected_beats if predicate(t)]
    if not eligible:
        return 1.0
    on_bar = sum(1 for t in eligible if distance_to_bar_line_bars(grid, t) <= on_bar_tolerance_bars)
    return on_bar / len(eligible)


def score_piecewise_maps(
    truth: PiecewiseMap,
    detected: PiecewiseMap,
    grid: BarGrid,
    *,
    truth_beats: Sequence[float] = (),
    detected_beats: Sequence[float] = (),
    tolerance_bars: float = 1.0,
    on_bar_tolerance_bars: float = DEFAULT_ON_BAR_TOLERANCE_BARS,
    values_equal: Callable[[object, object], bool] = _values_equal_default,
) -> SegmentMapScore:
    """Coverage from value agreement; phase from boundaries and beat bar lines."""
    coverage = coverage_fraction(truth, detected, values_equal=values_equal)
    boundary_recall = _boundary_phase_recall(truth, detected, grid, tolerance_bars=tolerance_bars)

    beat_on_bar: float | None = None
    has_truth_bounds = bool(_interior_boundaries(truth))
    has_beats = bool(detected_beats)

    if has_beats:
        beat_on_bar = _beat_on_bar_fraction(
            detected_beats,
            grid,
            on_bar_tolerance_bars=on_bar_tolerance_bars,
            predicate=lambda t: _values_match_at_time(truth, detected, t, values_equal),
        )

    if has_truth_bounds and has_beats:
        phase = min(boundary_recall, beat_on_bar if beat_on_bar is not None else 1.0)
    elif has_truth_bounds:
        phase = boundary_recall
    elif has_beats:
        phase = beat_on_bar if beat_on_bar is not None else 1.0
    else:
        phase = 1.0

    return SegmentMapScore(
        coverage=coverage,
        phase=phase,
        boundary_recall=boundary_recall,
        beat_on_bar=beat_on_bar,
    )


__all__ = [
    "DEFAULT_ON_BAR_TOLERANCE_BARS",
    "PiecewiseMap",
    "SegmentMapScore",
    "ValueSegment",
    "coverage_fraction",
    "score_piecewise_maps",
]
