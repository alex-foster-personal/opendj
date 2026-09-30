"""Constant-region grid fit: the longest steady stretch of beats sets the grid.

WHY A SECOND FITTER. `grid_fit.fit_line` fits one least-squares line through
every beat that lands near it, seeded on the longest run of clean intervals.
Mixxx's constant-BPM analyzer takes a different route that is well documented
and widely used for exactly the grids rekordbox serves (one BPM, one phase):

  1. REGIONS. Walk the model beats and cut them into regions inside which
     every beat sits within `PHASE_TOLERANCE_S` (25 ms) of the straight line
     from the region's first beat to its last. A missed, doubled or late beat
     ends a region instead of bending the fit.
  2. LONGEST. The longest region with at least `MIN_REGION_BEATS` (16) beats
     is the tempo reference: a whole steady stretch, not an average over
     breakdowns and fills.
  3. EXTEND. Every other region whose start or end lands on that reference
     line, at a whole number of beats and within the same tolerance, is
     merged into it, and the period is re-read over the longer span. A
     breakdown between two drops no longer stops the second drop from
     tightening the tempo.
  4. ROUND. The BPM is rounded to the coarsest DJ-software step the merged
     span allows (the same steps `grid_fit.round_bpm` tries, at this module's
     tolerance).
  5. PHASE. The anchor is the mean phase of every beat in the merged span
     against the rounded period, so no single beat decides where the grid
     sits.

Mixxx is GPL, so this is a re-implementation from that description (see
`/mnt/project-files/beatgrid-research/beatgrids-rust-and-dynamic-grids.md`
section 3), not a port of its source.

`fit_const_regions_piecewise` is the experiment toward piecewise grids
(#1481): every region long enough to stand on its own keeps its own line, and
the gaps between them are filled by extending their neighbors.

Both return `grid_fit.GridFit`, rendered, offset and bar-numbered by the same
`grid_fit.grid_from_lines` the line fitter uses, so the two fitters differ in
how they choose the line and nothing else.

A track with no steady span covering `MIN_COVERAGE` of it is not a constant
tempo, and the recipe has nothing to say about it; the served fit hands it to
`grid_fit.fit_grid`, which splits at tempo changes, and says so in
`GridFit.fitter`. Round 5 (`ops/beatbench/round-5/`) scores both halves.

-Claude
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from apps.analysis_beatgrid.grid_fit import (
    DEFAULT_OFFSET_S,
    GridFit,
    SegmentLine,
    fit_grid,
    grid_from_lines,
    round_bpm,
)

#: Largest distance a beat may sit from its region's line. 25 ms is the
#: tolerance the described recipe uses, a little over one 50 fps model frame
#: of quantization plus jitter.
PHASE_TOLERANCE_S = 0.025

#: Fewest beat INTERVALS a region needs to set the tempo: four bars.
MIN_REGION_BEATS = 16

#: Fraction of the beat list's time the merged reference span must cover for
#: the track to count as one constant tempo. Measured on the round-0 beats
#: (round 5): fixed-tempo fixtures cover median 1.00, p10 0.72; dynamic ones
#: median 0.00, p75 0.18. 0.5 sits in the gap; 0.7 and 0.9 were scored too
#: and hand fixed tracks with a long intro fill back to the line fitter.
MIN_COVERAGE = 0.5

#: `GridFit.fitter` for a grid this module chose the line for.
FITTER_CONST_REGIONS = "const_regions"

#: No region long enough to set the tempo (only without `fallback_line`).
REASON_NO_CONST_REGION = "const_regions_no_region"


@dataclass(frozen=True)
class ConstRegion:
    """Beats `lo..hi` (list indices, inclusive) on one straight line."""

    lo: int
    hi: int
    start_s: float
    end_s: float

    @property
    def n_beats(self) -> int:
        """Beat intervals spanned, which is what the tempo is read over."""
        return self.hi - self.lo

    @property
    def period_s(self) -> float:
        return (self.end_s - self.start_s) / self.n_beats


def _ends_line(ts: Sequence[float], lo: int, hi: int) -> tuple[float, float]:
    """`(t_lo, period)` of the line through the region's first and last beats."""
    return ts[lo], (ts[hi] - ts[lo]) / (hi - lo)


def _lsq_line(ts: Sequence[float], lo: int, hi: int) -> tuple[float, float]:
    """`(t_lo, period)` of the least-squares line through every beat in `lo..hi`."""
    n = hi - lo + 1
    mk = (n - 1) / 2.0
    mt = statistics.fmean(ts[lo : hi + 1])
    den = sum((k - mk) ** 2 for k in range(n))
    period = sum((k - mk) * (ts[lo + k] - mt) for k in range(n)) / den
    return mt - period * mk, period


def _fits(
    ts: Sequence[float], lo: int, hi: int, tolerance_s: float, least_squares: bool
) -> tuple[float, float] | None:
    """The region's line when every beat in `lo..hi` is within `tolerance_s` of it."""
    t0, period = (_lsq_line if least_squares else _ends_line)(ts, lo, hi)
    if not period > 0:
        return None
    if all(abs(ts[j] - (t0 + (j - lo) * period)) <= tolerance_s for j in range(lo, hi + 1)):
        return t0, period
    return None


def find_const_regions(
    beats: Sequence[float],
    tolerance_s: float = PHASE_TOLERANCE_S,
    *,
    least_squares: bool = False,
) -> list[ConstRegion]:
    """Cut the beat list into maximal straight regions, left to right.

    A region grows one beat at a time while every beat inside it stays within
    `tolerance_s` of the region's line. The beat that breaks it is where the
    next region starts, so consecutive regions share a boundary beat and the
    regions cover the whole list.

    THE LINE. The described recipe draws it through the region's first and
    last beats, and that is the default. That line inherits both endpoints'
    frame error, so a steady 124 BPM track with 6 ms jitter breaks into eight
    regions at 25 ms; the extension step merges them back. `least_squares=True`
    fits each region's line over all its beats instead. Round 5 scored both:
    least squares finds a 16-beat region on 14 more of 337 fixtures, and once
    gated by `MIN_COVERAGE` the two score within 1 point of each other on
    every fixed-tempo KPI, so the recipe as described is what is served.
    `start_s` and `end_s` are the LINE's times at the region's ends.
    """
    ts = [float(t) for t in beats]
    regions: list[ConstRegion] = []
    if len(ts) < 2:
        return regions
    lo = 0
    while lo < len(ts) - 1:
        hi = lo + 1
        line = _fits(ts, lo, hi, tolerance_s, least_squares)
        if line is None:  # two beats not in time order: a region of its own
            regions.append(ConstRegion(lo, hi, ts[lo], ts[hi]))
            lo = hi
            continue
        while hi + 1 < len(ts):
            wider = _fits(ts, lo, hi + 1, tolerance_s, least_squares)
            if wider is None:
                break
            hi, line = hi + 1, wider
        t0, period = line
        regions.append(ConstRegion(lo, hi, t0, t0 + (hi - lo) * period))
        lo = hi
    return regions


@dataclass
class _Span:
    """The reference line while regions are merged into it."""

    start_s: float
    end_s: float
    n_beats: int
    members: list[ConstRegion]

    @property
    def period_s(self) -> float:
        return (self.end_s - self.start_s) / self.n_beats


def _try_merge(span: _Span, region: ConstRegion, before: bool, tolerance_s: float) -> _Span | None:
    """`span` widened to take in `region`, or None when it does not fit.

    The far end of `region` must land a whole number of reference beats away,
    close enough that the widened span's period keeps every endpoint of the
    old span and of `region` within `tolerance_s` of the widened line.
    """
    period = span.period_s
    gap_s = span.end_s - region.start_s if before else region.end_s - span.start_s
    n = round(gap_s / period)
    if n <= span.n_beats:
        return None
    start = region.start_s if before else span.start_s
    end = span.end_s if before else region.end_s
    new_period = (end - start) / n
    # Every member's ends, not just the outer ones, must still sit on the
    # widened line: widening moves the period, which can push a region merged
    # earlier off it.
    for member in (*span.members, region):
        for t in (member.start_s, member.end_s):
            k = round((t - start) / new_period)
            if abs(t - (start + k * new_period)) > tolerance_s:
                return None
    # The region's own tempo must agree too, or a short region whose ends
    # happen to land on the line would drag a fill's tempo into the grid.
    if abs(region.period_s - new_period) * region.n_beats > tolerance_s:
        return None
    members = [region, *span.members] if before else [*span.members, region]
    return _Span(start, end, n, members)


def _extend(
    regions: Sequence[ConstRegion], i_ref: int, tolerance_s: float, stop: tuple[float, float]
) -> _Span:
    """Grow the reference region outward over every neighbor that fits.

    A region that does not fit is skipped rather than ending the walk, so a
    breakdown between two drops does not keep the second drop out. `stop`
    bounds the walk in time, which the piecewise fit uses to keep each line
    between its neighbors.
    """
    ref = regions[i_ref]
    span = _Span(ref.start_s, ref.end_s, ref.n_beats, [ref])
    for j in range(i_ref - 1, -1, -1):
        if regions[j].start_s < stop[0]:
            break
        merged = _try_merge(span, regions[j], True, tolerance_s)
        if merged is not None:
            span = merged
    for j in range(i_ref + 1, len(regions)):
        if regions[j].end_s > stop[1]:
            break
        merged = _try_merge(span, regions[j], False, tolerance_s)
        if merged is not None:
            span = merged
    return span


def _line_for_span(
    ts: Sequence[float], span: _Span, rounding: bool, tolerance_s: float, cover: tuple[float, float]
) -> SegmentLine:
    """The rounded, phase-averaged line through `span`, rendered over `cover`."""
    fitted = 60.0 / span.period_s
    bpm, step = fitted, None
    if rounding:
        bpm, step = round_bpm(fitted, span.n_beats, tolerance_s=tolerance_s)
    period = 60.0 / bpm
    # Phase: the mean of every member beat's offset from the rounded grid,
    # indexed from the span's first beat.
    idx = sorted({j for r in span.members for j in range(r.lo, r.hi + 1)})
    ks = [round((ts[j] - span.start_s) / period) for j in idx]
    residuals = [ts[j] - k * period for j, k in zip(idx, ks, strict=True)]
    anchor = statistics.fmean(residuals)
    rms = statistics.fmean((r - anchor) ** 2 for r in residuals) ** 0.5
    # Beats whose phase against the line is off by more than the tolerance
    # (a doubled beat inside a merged neighbor) are not counted as inliers.
    n_in = sum(1 for r in residuals if abs(r - anchor) <= tolerance_s)
    return SegmentLine(
        anchor_s=anchor,
        bpm=bpm,
        bpm_fitted=fitted,
        round_step=step,
        k_lo=round((cover[0] - anchor) / period),
        k_hi=round((cover[1] - anchor) / period),
        n_inliers=n_in,
        n_outliers=len(ts) - n_in,
        residual_rms_s=rms,
    )


def _longest(regions: Sequence[ConstRegion], min_beats: int) -> int | None:
    """Index of the longest region (by duration) with at least `min_beats`."""
    best: int | None = None
    for i, r in enumerate(regions):
        if r.n_beats < min_beats:
            continue
        if best is None or r.end_s - r.start_s > regions[best].end_s - regions[best].start_s:
            best = i
    return best


def const_lines(
    beats: Sequence[float],
    *,
    rounding: bool = True,
    tolerance_s: float = PHASE_TOLERANCE_S,
    min_region_beats: int = MIN_REGION_BEATS,
    min_coverage: float = 0.0,
    least_squares: bool = False,
) -> list[SegmentLine] | None:
    """The one constant-tempo line for the track, or None without a long region
    or when the merged span covers less than `min_coverage` of the beats' time."""
    ts = [float(t) for t in beats]
    regions = find_const_regions(ts, tolerance_s, least_squares=least_squares)
    i_ref = _longest(regions, min_region_beats)
    if i_ref is None:
        return None
    span = _extend(regions, i_ref, tolerance_s, (float("-inf"), float("inf")))
    if (span.end_s - span.start_s) < min_coverage * (ts[-1] - ts[0]):
        return None
    return [_line_for_span(ts, span, rounding, tolerance_s, (ts[0], ts[-1]))]


def _place_spans(
    regions: Sequence[ConstRegion], order: Sequence[int], tolerance_s: float
) -> list[_Span]:
    """Extend each region in `order` within the gap the spans already placed
    leave it, skipping a region an earlier span already covers; sorted by time."""
    spans: list[_Span] = []
    for i in order:
        r = regions[i]
        if any(s.start_s <= r.start_s < s.end_s or s.start_s < r.end_s <= s.end_s for s in spans):
            continue
        left = max((s.end_s for s in spans if s.end_s <= r.start_s), default=float("-inf"))
        right = min((s.start_s for s in spans if s.start_s >= r.end_s), default=float("inf"))
        spans.append(_extend(regions, i, tolerance_s, (left, right)))
    return sorted(spans, key=lambda s: s.start_s)


def piecewise_lines(
    beats: Sequence[float],
    *,
    rounding: bool = True,
    tolerance_s: float = PHASE_TOLERANCE_S,
    min_region_beats: int = MIN_REGION_BEATS,
    least_squares: bool = False,
) -> list[SegmentLine] | None:
    """One line per long region after merging, gaps filled by the neighbors.

    Longest region first: it is extended over whatever fits, then the next
    longest region not already absorbed is extended within the time left
    between the lines already placed, and so on. Each line covers from the
    midpoint of the gap before it to the midpoint of the gap after it.
    """
    ts = [float(t) for t in beats]
    regions = find_const_regions(ts, tolerance_s, least_squares=least_squares)
    order = sorted(
        (i for i, r in enumerate(regions) if r.n_beats >= min_region_beats),
        key=lambda i: regions[i].start_s - regions[i].end_s,
    )
    if not order:
        return None
    spans = _place_spans(regions, order, tolerance_s)
    lines: list[SegmentLine] = []
    for n, s in enumerate(spans):
        lo = ts[0] if n == 0 else 0.5 * (spans[n - 1].end_s + s.start_s)
        hi = ts[-1] if n == len(spans) - 1 else 0.5 * (s.end_s + spans[n + 1].start_s)
        lines.append(_line_for_span(ts, s, rounding, tolerance_s, (lo, hi)))
    return lines


def fit_const_regions(
    beats: Sequence[float],
    downbeats: Sequence[float],
    *,
    rounding: bool = True,
    offset_s: float = DEFAULT_OFFSET_S,
    min_coverage: float = MIN_COVERAGE,
    fallback_line: bool = True,
    piecewise: bool = False,
    least_squares: bool = False,
) -> GridFit:
    """Regularize one track's model beats with the constant-region recipe.

    Same contract as `grid_fit.fit_grid`: a `GridFit` with the served beats
    and bar numbers, or a reason and no beats. When no region of
    `MIN_REGION_BEATS` extends over `min_coverage` of the track, the track is
    not one steady tempo, and with `fallback_line` (the served behavior) the
    line fitter, which splits at tempo changes, grids it instead; the result
    then says `fitter="line"`. Without it the track fails with
    `REASON_NO_CONST_REGION`, which is the recipe measured on its own.
    """
    ts = [float(t) for t in beats]
    if piecewise:
        lines = piecewise_lines(ts, rounding=rounding, least_squares=least_squares)
    else:
        lines = const_lines(
            ts, rounding=rounding, min_coverage=min_coverage, least_squares=least_squares
        )
    if lines:
        return grid_from_lines(lines, downbeats, offset_s, fitter=FITTER_CONST_REGIONS)
    if fallback_line:
        return fit_grid(ts, downbeats, rounding=rounding, offset_s=offset_s)
    return GridFit([], [], (), offset_s, REASON_NO_CONST_REGION, fitter=FITTER_CONST_REGIONS)


__all__ = [
    "FITTER_CONST_REGIONS",
    "MIN_COVERAGE",
    "MIN_REGION_BEATS",
    "PHASE_TOLERANCE_S",
    "REASON_NO_CONST_REGION",
    "ConstRegion",
    "const_lines",
    "find_const_regions",
    "fit_const_regions",
    "piecewise_lines",
]
