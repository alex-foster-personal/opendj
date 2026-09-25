"""Boundary-F@1bar scoring for the NATIVE-05 GiantSteps+ measurement.

WHAT THIS IS. `specs/native-analysis-v1.md` section 5's key-change arm (a):
"boundary F at 1 bar ONLY on verified timestamped annotations". This module is
the scorer, pure and audio-free -- it takes detected and annotated boundary
TIMES plus a real bar grid, and answers whether they align. The real audio,
the real `own_beatgrid`/`own_key` producers and the fixture live in
`tests/analysis_key/test_giantsteps_plus_boundary.py`; this module is
unit-tested on plain floats in `test_giantsteps_scoring.py` so its own
correctness (does "within 1 bar" mean what it claims) is never entangled with
whether the shipping analyzer currently gets a boundary right.

WHY THE TOLERANCE IS A REAL BAR, NOT A FIXED SECOND COUNT. Spec section 5 says
"within 1 bar", and a bar's duration depends on the track's own tempo at that
point in time (EDM is mostly fixed-tempo, but this stays correct under drift
too). `bar_duration_at` walks the real `BarGrid` the own beatgrid producer
returned and reads off the bar covering the queried time, so the tolerance a
132 BPM track gets is shorter than the one a 90 BPM track gets, which is what
"1 bar" actually means.

BOUNDARY MATCHING IS GREEDY AND ONE-TO-ONE. Two annotated boundaries could
both fall within tolerance of the same detected one (or vice versa) on a
messy real signal; a naive "any pair within tolerance counts" double-counts.
Matching nearest-first and removing both sides once matched keeps precision
and recall meaningful counts rather than inflated ones.

-Claude Sonnet 5
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from apps.analysis_key.segments import BarGrid


def bar_duration_at(grid: BarGrid, t: float) -> float:
    """The duration in seconds of the bar covering time `t`.

    `t` at or past the grid's last bar start uses the last bar's duration
    (there is no bar after it to be "covering" it, and the last bar's own
    extent is the only honest answer). Raises if the grid has no bars at all,
    since there is then no honest bar duration to report.
    """
    if grid.n_bars == 0:
        raise ValueError("a bar grid with zero bars has no bar duration")
    for start, end in zip(grid.starts, grid.ends, strict=True):
        if start <= t < end:
            return end - start
    return grid.ends[-1] - grid.starts[-1]


def boundary_within_tolerance(
    detected_s: float, annotated_s: float, grid: BarGrid, *, tolerance_bars: float = 1.0
) -> bool:
    """Whether `detected_s` lands within `tolerance_bars` real bars of `annotated_s`.

    The tolerance is measured in the bar covering the ANNOTATED time: that is
    the ground truth the spec's "1 bar" refers to, not whatever bar the
    (possibly wrong) detected time happens to fall in.
    """
    tolerance_s = tolerance_bars * bar_duration_at(grid, annotated_s)
    return abs(detected_s - annotated_s) <= tolerance_s


@dataclass(frozen=True)
class BoundaryScore:
    """One track's boundary-F@1bar result, every count named."""

    n_annotated: int
    n_detected: int
    n_matched: int

    @property
    def precision(self) -> float:
        return self.n_matched / self.n_detected if self.n_detected else 0.0

    @property
    def recall(self) -> float:
        return self.n_matched / self.n_annotated if self.n_annotated else 0.0

    @property
    def f_measure(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0


def score_track(
    detected_boundaries_s: Sequence[float],
    annotated_boundaries_s: Sequence[float],
    grid: BarGrid,
    *,
    tolerance_bars: float = 1.0,
) -> BoundaryScore:
    """Greedy nearest-first one-to-one matching within `tolerance_bars`."""
    remaining_detected = list(detected_boundaries_s)
    matched = 0
    for annotated in annotated_boundaries_s:
        candidates = [
            d for d in remaining_detected
            if boundary_within_tolerance(d, annotated, grid, tolerance_bars=tolerance_bars)
        ]
        if not candidates:
            continue
        nearest = min(candidates, key=lambda d: abs(d - annotated))
        remaining_detected.remove(nearest)
        matched += 1
    return BoundaryScore(
        n_annotated=len(annotated_boundaries_s),
        n_detected=len(detected_boundaries_s),
        n_matched=matched,
    )


__all__ = ["BoundaryScore", "bar_duration_at", "boundary_within_tolerance", "score_track"]
