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

WHY THE TOLERANCE IS A BAR-COORDINATE DISTANCE, NOT A SYMMETRIC SECOND COUNT
(P1 BLOCKING, sol-review #3948). The original implementation converted "1 bar"
to a SECONDS tolerance using only the ANNOTATED time's bar duration, then
compared it against a plain `abs(detected_s - annotated_s)` in seconds. On a
variable-tempo grid that is not "within one bar" at all: a detection exactly
one bar away, where that one bar happens to be a LONG bar, could exceed a
SHORT annotated bar's seconds tolerance and be wrongly rejected; a detection
several SHORT bars away could fit inside a LONG annotated bar's generous
seconds tolerance and be wrongly accepted. Both directions are real bugs
because "1 bar" is a statement about the GRID, not about elapsed seconds.
`bar_position_at` maps a time to its fractional bar coordinate (bar index plus
fraction through that bar) by walking the same real grid, and
`boundary_within_tolerance` now compares the two times' bar-coordinate
DISTANCE against `tolerance_bars`, which is what "within 1 bar" means on a
grid whose bar duration moves. A time before the grid's first bar or at/after
its final extent extrapolates using the first/last bar's own duration, the
same edge rule `bar_duration_at` already used.

BOUNDARY MATCHING IS MAXIMUM-CARDINALITY, NOT GREEDY. Two annotated
boundaries could both fall within tolerance of the same detected one (or vice
versa) on a messy real signal; a naive "any pair within tolerance counts"
double-counts. The one-to-one fix has to be a real maximum bipartite matching
(Kuhn's algorithm, augmenting paths), not "process annotations in input order,
grab each one's nearest still-free detection" -- that greedy version can
UNDERCOUNT: annotated at 10s/12s, detected at 8.5s/11s, tolerance 2s. Both
10<->8.5 (dist 1.5) and 12<->11 (dist 1) are individually valid, and together
form a size-2 matching. Greedy nearest-first processes 10 first, sees both
8.5 (dist 1.5) and 11 (dist 1) as candidates, takes the nearer one (11), which
then starves 12 (only 8.5 is left, dist 3.5, outside tolerance) -- reporting
matched=1 when a valid matched=2 assignment exists (sol-review #3948 P1
BLOCKING, `discussion_r3948_giantsteps_scoring`). Kuhn's algorithm finds the
true maximum regardless of input order.

-Claude Sonnet 5
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from apps.analysis_key.segments import BarGrid


def bar_duration_at(grid: BarGrid, t: float) -> float:
    """The duration in seconds of the bar covering time `t`.

    `t` before the grid's first bar start uses the first bar's duration (no bar
    covers that time yet, but the first bar is the only honest pre-grid
    reference). `t` at or past the grid's last bar start uses the last bar's
    duration (there is no bar after it to be "covering" it, and the last bar's
    own extent is the only honest answer). Raises if the grid has no bars at
    all, since there is then no honest bar duration to report.
    """
    if grid.n_bars == 0:
        raise ValueError("a bar grid with zero bars has no bar duration")
    if t < grid.starts[0]:
        return grid.ends[0] - grid.starts[0]
    for start, end in zip(grid.starts, grid.ends, strict=True):
        if start <= t < end:
            return end - start
    return grid.ends[-1] - grid.starts[-1]


def bar_position_at(grid: BarGrid, t: float) -> float:
    """`t` as a fractional bar coordinate: bar index plus fraction through it.

    Bar `i` covers the half-open coordinate range `[i, i + 1)`, so two times
    in the same bar are less than 1.0 apart and two times exactly one bar
    apart (same fraction, adjacent bar) are exactly 1.0 apart, regardless of
    how long either bar actually is in seconds. `t` before the grid's first
    bar start extrapolates backward using the first bar's own duration; `t`
    at or past the grid's last bar's end extrapolates forward using the last
    bar's own duration -- the same edge rule `bar_duration_at` uses, so a time
    outside the grid still gets an honest position rather than clamping to
    bar 0 or the last bar.
    """
    if grid.n_bars == 0:
        raise ValueError("a bar grid with zero bars has no bar position")
    if t < grid.starts[0]:
        first_bar_s = grid.ends[0] - grid.starts[0]
        return (t - grid.starts[0]) / first_bar_s
    for index, (start, end) in enumerate(zip(grid.starts, grid.ends, strict=True)):
        if start <= t < end:
            return index + (t - start) / (end - start)
    last_index = grid.n_bars - 1
    last_bar_s = grid.ends[last_index] - grid.starts[last_index]
    return last_index + (t - grid.starts[last_index]) / last_bar_s


def boundary_within_tolerance(
    detected_s: float, annotated_s: float, grid: BarGrid, *, tolerance_bars: float = 1.0
) -> bool:
    """Whether `detected_s` lands within `tolerance_bars` real bars of `annotated_s`.

    Both times are mapped to fractional bar coordinates on the same grid
    (`bar_position_at`) and compared by their coordinate DISTANCE, not by a
    seconds tolerance derived from one side's bar duration -- see the module
    docstring for why a variable-tempo grid needs this.
    """
    detected_bar_position = bar_position_at(grid, detected_s)
    annotated_bar_position = bar_position_at(grid, annotated_s)
    return abs(detected_bar_position - annotated_bar_position) <= tolerance_bars


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


def _max_cardinality_matched_count(
    annotated_boundaries_s: Sequence[float],
    detected_boundaries_s: Sequence[float],
    grid: BarGrid,
    *,
    tolerance_bars: float,
) -> int:
    """True maximum one-to-one matching count via Kuhn's algorithm.

    `adjacency[a_idx]` lists every detected index within tolerance of
    annotated boundary `a_idx`. Each annotated index tries to claim a free
    (or re-augmentable) detected index by depth-first search over alternating
    paths; this finds the graph's true maximum matching regardless of the
    order boundaries arrive in, unlike a single greedy nearest-first pass.
    """
    adjacency = [
        [
            d_idx for d_idx, detected in enumerate(detected_boundaries_s)
            if boundary_within_tolerance(detected, annotated, grid, tolerance_bars=tolerance_bars)
        ]
        for annotated in annotated_boundaries_s
    ]
    owner_of_detected: list[int | None] = [None] * len(detected_boundaries_s)

    def try_augment(a_idx: int, visited: set[int]) -> bool:
        for d_idx in adjacency[a_idx]:
            if d_idx in visited:
                continue
            visited.add(d_idx)
            holder = owner_of_detected[d_idx]
            if holder is None or try_augment(holder, visited):
                owner_of_detected[d_idx] = a_idx
                return True
        return False

    return sum(try_augment(a_idx, set()) for a_idx in range(len(annotated_boundaries_s)))


def score_track(
    detected_boundaries_s: Sequence[float],
    annotated_boundaries_s: Sequence[float],
    grid: BarGrid,
    *,
    tolerance_bars: float = 1.0,
) -> BoundaryScore:
    """Maximum-cardinality one-to-one matching within `tolerance_bars`."""
    matched = _max_cardinality_matched_count(
        annotated_boundaries_s, detected_boundaries_s, grid, tolerance_bars=tolerance_bars
    )
    return BoundaryScore(
        n_annotated=len(annotated_boundaries_s),
        n_detected=len(detected_boundaries_s),
        n_matched=matched,
    )


__all__ = [
    "BoundaryScore",
    "bar_duration_at",
    "bar_position_at",
    "boundary_within_tolerance",
    "score_track",
]
