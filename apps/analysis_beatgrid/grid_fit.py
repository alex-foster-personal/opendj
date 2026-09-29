"""Regularized grid: model beats replaced by the straight line they lie on.

WHY. The served own grid used to be Beat This!'s raw peak times. Those land on
the model's 50 fps frames (20 ms steps) and carry its per-beat jitter, while a
rekordbox static grid is a straight line: one BPM at 0.01 precision and one
phase. Round 0 put the cost of that difference on the table: fixed-tempo F
0.857 as served against 0.942 once each track's median offset is removed
(`ops/beatbench/round-0/report-rescored-v1.1.0.md`). This module closes it in
three steps, each measured separately by `scripts/beatbench/run_grid_fit.py`:

  1. LINE. Each constant-tempo segment (from `tempo_change.detect_tempo_changes`)
     becomes `t_k = anchor + k * period`, fitted by least squares over the
     segment's beats. Beat INDICES are assigned from the spacing, not from list
     position, so a beat the model missed or doubled moves no later beat, and
     beats more than `OUTLIER_S` off the line are dropped before the refit.
  2. ROUND. The fitted BPM is snapped to the coarsest DJ-software step
     (whole, half, tenth, hundredth BPM) whose line stays within
     `ROUND_TOLERANCE_S` of the fitted one across the segment. 164 of the 200
     fixed rekordbox grids in the round-0 fixture set are whole-BPM. The idea
     follows Mixxx's `BeatUtils::roundBpmWithinRange`; Mixxx is GPL, so this is
     a re-implementation from the description, not a copy.
  3. OFFSET. A constant shift is added to every beat. Beat This! beats sit
     early against human annotation (round 2, GTZAN, median -8 ms), so the
     default is `DEFAULT_OFFSET_S`; the bench reports it on and off.

Bar numbers are voted on the LINE index (downbeat's nearest k, mod 4) rather
than on list position, for the same missed-beat reason, with the same floor as
`bar_phase.lock_bar_phase`.

Pure and stdlib-only, like the rest of the fitter, so the bench, the lane
builder and a later Rust port can all read the same arithmetic.

-Claude
"""

from __future__ import annotations

import bisect
import itertools
import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field

from apps.analysis_beatgrid.bar_phase import (
    BAR_BEATS,
    BAR_PHASE_AGREEMENT_FLOOR,
    DOUBLE_MIN_GAP_BEATS,
)
from apps.analysis_beatgrid.tempo_change import MIN_RELATIVE_BPM_DELTA, detect_tempo_changes

#: The two grid-fit modes. `raw` is the served behavior before this module.
GRID_FIT_RAW = "raw"
GRID_FIT_LINE = "line"
GRID_FIT_MODES = (GRID_FIT_RAW, GRID_FIT_LINE)

#: A beat further than this from the fitted line is not used to fit it. Half
#: the 70 ms scoring tolerance: a beat this far off is a model error, not jitter.
OUTLIER_S = 0.035

#: Largest drift the rounded line may show against the fitted one, at either
#: end of the segment. Well inside the 70 ms scoring tolerance and about one
#: model frame, so rounding never moves a beat by more than the model's own
#: resolution.
ROUND_TOLERANCE_S = 0.020

#: BPM steps tried coarsest first. 0.01 is PQTZ's own resolution, so the last
#: step always succeeds and the result is always a legal rekordbox tempo.
ROUND_STEPS = (1.0, 0.5, 0.1, 0.01)

#: Constant shift added to every regularized beat. Round 2 (GTZAN, 998 clips,
#: `ops/beatbench/round-2/`) measured Beat This! beats a median 8 ms EARLY
#: against human annotation, so they are moved later by that much. Taken from
#: human truth rather than from the rekordbox fixtures this is scored on, so
#: the bench does not grade a constant it was tuned to.
DEFAULT_OFFSET_S = 0.008

#: A model interval within this of the median counts as clean when seeding the
#: line: two 50 fps frames, the model's own quantization plus one frame of jitter.
CLEAN_INTERVAL_S = 0.040

#: Fewest inlier beats a segment line is fitted from.
MIN_LINE_BEATS = 8

REASON_TOO_FEW_BEATS = "grid_fit_too_few_beats"
REASON_BAR_PHASE_BELOW_FLOOR = "grid_fit_bar_phase_below_floor"
REASON_NO_DOWNBEATS = "grid_fit_no_downbeats"


@dataclass(frozen=True)
class SegmentLine:
    """One constant-tempo stretch: `t_k = anchor_s + k * 60 / bpm` for k in [k_lo, k_hi]."""

    anchor_s: float
    bpm: float
    bpm_fitted: float
    round_step: float | None
    k_lo: int
    k_hi: int
    n_inliers: int
    n_outliers: int
    residual_rms_s: float

    def time_of(self, k: int) -> float:
        return self.anchor_s + k * 60.0 / self.bpm


@dataclass(frozen=True)
class GridFit:
    """The regularized grid, or the reason there is none."""

    beats: list[float]
    beat_numbers: list[int]
    lines: tuple[SegmentLine, ...]
    offset_s: float
    reason: str | None = None
    phase_agreement: float | None = None
    #: Per served beat, the index into `lines` of the segment it belongs to.
    beat_lines: list[int] = field(default_factory=list)


def _clean_run(ts: Sequence[float], period: float) -> tuple[int, int]:
    """Longest run of consecutive beats whose every interval is within
    `CLEAN_INTERVAL_S` of `period`: the stretch where list position IS the
    beat index, so it can seed the line without any index guessing."""
    best = (0, 0)
    start = 0
    for i in range(1, len(ts)):
        if abs((ts[i] - ts[i - 1]) - period) > CLEAN_INTERVAL_S:
            start = i
        elif i - start > best[1] - best[0]:
            best = (start, i)
    return best


def _inliers(ts: Sequence[float], a: float, p: float) -> tuple[list[int], list[float]]:
    """Beats within `OUTLIER_S` of the line, one per grid index (the closest)."""
    best: dict[int, tuple[float, float]] = {}
    for t in ts:
        k = round((t - a) / p)
        r = abs(t - (a + k * p))
        if r <= OUTLIER_S and (k not in best or r < best[k][0]):
            best[k] = (r, t)
    ks = sorted(best)
    return ks, [best[k][1] for k in ks]


def _ls(ks: Sequence[int], ts: Sequence[float]) -> tuple[float, float]:
    """Least-squares `t = a + p * k`; returns `(a, p)`."""
    n = len(ks)
    mk = sum(ks) / n
    mt = sum(ts) / n
    den = sum((k - mk) ** 2 for k in ks)
    p = sum((k - mk) * (t - mt) for k, t in zip(ks, ts, strict=True)) / den
    return mt - p * mk, p


def round_bpm(
    bpm: float, k_span: float, tolerance_s: float = ROUND_TOLERANCE_S
) -> tuple[float, float]:
    """Coarsest step whose line drifts at most `tolerance_s` over half the span.

    The rounded line is re-anchored at the segment's centre by the caller, so
    the worst drift is at either end, `k_span / 2` beats away.
    """
    period = 60.0 / bpm
    half = max(k_span, 1.0) / 2.0
    for step in ROUND_STEPS:
        cand = round(bpm / step) * step
        if cand <= 0:
            continue
        if abs(60.0 / cand - period) * half <= tolerance_s:
            return round(cand, 2), step
    return round(bpm, 2), ROUND_STEPS[-1]


def _robust_ls(ts: Sequence[float]) -> tuple[float, float] | None:
    """`(anchor, period)` seeded on the longest clean run, then extended to
    every beat that lands on the line and refitted; None when too few do."""
    if len(ts) < MIN_LINE_BEATS:
        return None
    period = statistics.median(b - a for a, b in itertools.pairwise(ts))
    if not period > 0:
        return None
    lo, hi = _clean_run(ts, period)
    if hi - lo + 1 < MIN_LINE_BEATS:
        return None
    a, p = _ls(range(hi - lo + 1), ts[lo : hi + 1])
    for _ in range(3):
        kin, tin = _inliers(ts, a, p)
        if len(kin) < MIN_LINE_BEATS:
            return None
        a, p = _ls(kin, tin)
    return (a, p) if p > 0 else None


def fit_line(
    times: Sequence[float], *, rounding: bool = True, octave_multiple: float = 1.0
) -> SegmentLine | None:
    """One robust line through `times`, or None when too few beats survive.

    `octave_multiple` is `bpm.estimate_bpm`'s octave policy: the line is fitted
    at the model's metrical level, then published (rounded and rendered) at
    `fitted * octave_multiple`, so the served spacing IS the served tempo.
    """
    ts = [float(t) for t in times]
    seeded = _robust_ls(ts)
    if seeded is None:
        return None
    a, p = seeded
    kin, tin = _inliers(ts, a, p)
    fitted = 60.0 / p * octave_multiple
    bpm, step = fitted, None
    if rounding:
        bpm, step = round_bpm(fitted, (kin[-1] - kin[0]) * octave_multiple)
    period_used = 60.0 / bpm
    # Model index k sits at published index k * octave_multiple (fractional
    # when halving, which the mean below does not mind). Re-anchor with the
    # chosen period: mean residual zero over the inliers.
    kpub = [k * octave_multiple for k in kin]
    anchor = statistics.fmean(t - k * period_used for k, t in zip(kpub, tin, strict=True))
    rms = math.sqrt(
        statistics.fmean(
            (t - anchor - k * period_used) ** 2 for k, t in zip(kpub, tin, strict=True)
        )
    )
    return SegmentLine(
        anchor_s=anchor,
        bpm=bpm,
        bpm_fitted=fitted,
        round_step=step,
        k_lo=round((ts[0] - anchor) / period_used),
        k_hi=round((ts[-1] - anchor) / period_used),
        n_inliers=len(kin),
        n_outliers=len(ts) - len(kin),
        residual_rms_s=rms,
    )


def _material_split(
    beats: Sequence[float],
    bounds: Sequence[tuple[int, int]],
    rounding: bool,
    octave_multiple: float,
) -> list[SegmentLine] | None:
    """One line per proposed segment, or None when any segment cannot be
    fitted or no neighbouring pair differs by `MIN_RELATIVE_BPM_DELTA`."""
    lines: list[SegmentLine] = []
    for lo, hi in bounds:
        line = fit_line(beats[lo:hi], rounding=rounding, octave_multiple=octave_multiple)
        if line is None:
            return None
        lines.append(line)
    fitted = [line.bpm_fitted for line in lines]
    material = any(
        abs(x - y) / min(x, y) >= MIN_RELATIVE_BPM_DELTA for x, y in itertools.pairwise(fitted)
    )
    return lines if material else None


def _segment_lines(
    beats: Sequence[float], rounding: bool, octave_multiple: float
) -> list[SegmentLine] | None:
    """One line for the track unless its segments really differ in tempo.

    `detect_tempo_changes` splits on inter-beat intervals, so a breakdown full
    of spurious model beats can read as a tempo change. Each proposed segment
    is fitted robustly; the split is kept only when neighbouring segments'
    fitted tempos differ by `MIN_RELATIVE_BPM_DELTA`, the same materiality
    threshold the detector itself uses. Otherwise the whole track is one line,
    which is what a rekordbox static grid is.
    """
    analysis = detect_tempo_changes(beats)
    bounds = [(lo, hi) for lo, hi, _ in analysis.segments]
    if len(bounds) > 1:
        split = _material_split(beats, bounds, rounding, octave_multiple)
        if split is not None:
            return split
    line = fit_line(beats, rounding=rounding, octave_multiple=octave_multiple)
    return None if line is None else [line]


def _render(lines: Sequence[SegmentLine], offset_s: float) -> tuple[list[float], list[int]]:
    """Beat times from each line, cut where the next segment's line takes over.

    Returns the times and, per beat, the index of the line it came from. A
    beat the offset would put before 0 s is dropped: the deck refuses t < 0.
    """
    out: list[float] = []
    owner: list[int] = []
    for i, line in enumerate(lines):
        half = 0.5 * 60.0 / line.bpm
        end = lines[i + 1].time_of(lines[i + 1].k_lo) if i + 1 < len(lines) else math.inf
        for k in range(line.k_lo, line.k_hi + 1):
            t = line.time_of(k)
            if out and t <= out[-1] + half:
                continue
            if t >= end - half:
                break
            out.append(t)
            owner.append(i)
    kept = [(t + offset_s, i) for t, i in zip(out, owner, strict=True) if t + offset_s >= 0.0]
    return [t for t, _ in kept], [i for _, i in kept]


def _bar_numbers(
    beats: Sequence[float], downbeats: Sequence[float]
) -> tuple[list[int], float] | None:
    """1..4 per beat from the model's downbeats, voted on the GRID index.

    Same policy as `bar_phase.lock_bar_phase` (thin doubled downbeats closer
    than `DOUBLE_MIN_GAP_BEATS`, majority phase, ties to the smallest phase),
    but each downbeat is placed by its nearest grid beat rather than by its
    position in the model's beat list, so a missed beat cannot flip the bar.
    """
    if not downbeats or not beats:
        return None
    anchors = sorted({_nearest(beats, d) for d in downbeats})
    kept = [anchors[0]]
    for a in anchors[1:]:
        if a - kept[-1] >= DOUBLE_MIN_GAP_BEATS:
            kept.append(a)
    votes = [0] * BAR_BEATS
    for a in kept:
        votes[a % BAR_BEATS] += 1
    chosen = votes.index(max(votes))
    agreement = votes[chosen] / len(kept)
    numbers = [((i - chosen) % BAR_BEATS) + 1 for i in range(len(beats))]
    return numbers, agreement


def _nearest(beats: Sequence[float], t: float) -> int:
    i = bisect.bisect_left(beats, t)
    if i == 0:
        return 0
    if i == len(beats):
        return i - 1
    return i if beats[i] - t < t - beats[i - 1] else i - 1


def fit_grid(
    beats: Sequence[float],
    downbeats: Sequence[float],
    *,
    rounding: bool = True,
    offset_s: float = DEFAULT_OFFSET_S,
    octave_multiple: float = 1.0,
) -> GridFit:
    """Regularize one track's model beats into a served grid.

    `octave_multiple` (from `bpm.estimate_bpm`) renders the grid at the
    published metrical level, so a model that tracked 67.5 BPM for a track the
    octave policy publishes at 135 serves beats 60/135 s apart.
    """
    ts = [float(t) for t in beats]
    if len(ts) < MIN_LINE_BEATS:
        return GridFit([], [], (), offset_s, REASON_TOO_FEW_BEATS)
    lines = _segment_lines(ts, rounding, octave_multiple)
    if not lines:
        return GridFit([], [], (), offset_s, REASON_TOO_FEW_BEATS)
    grid, owner = _render(lines, offset_s)
    # Vote on the un-shifted grid: downbeats are model times, like the beats were.
    bars = _bar_numbers([t - offset_s for t in grid], [float(d) for d in downbeats])
    if bars is None:
        return GridFit([], [], tuple(lines), offset_s, REASON_NO_DOWNBEATS)
    numbers, agreement = bars
    if agreement < BAR_PHASE_AGREEMENT_FLOOR:
        return GridFit([], [], tuple(lines), offset_s, REASON_BAR_PHASE_BELOW_FLOOR, agreement)
    return GridFit(grid, numbers, tuple(lines), offset_s, None, agreement, owner)


__all__ = [
    "DEFAULT_OFFSET_S",
    "GRID_FIT_LINE",
    "GRID_FIT_MODES",
    "GRID_FIT_RAW",
    "GridFit",
    "SegmentLine",
    "fit_grid",
    "fit_line",
    "round_bpm",
]
