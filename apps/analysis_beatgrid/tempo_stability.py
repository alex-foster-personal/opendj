"""Tempo stability: where in a track the tempo moves, which way, and how far.

One answer for three readers (issue #8339): the deck strip's tempo lane, the
big waveform's tempo-change marks, and the library's BPM Stability column
(issue #7879). Each reads the same `TempoStability`, so none of them computes
a verdict of its own (the #7879 veto: the frontend never decides).

INPUT IS THE SERVED GRID. The caller hands in the beat times the deck draws
(`/anlz` beatgrid, grid edits included), whichever source made them. That is
why a rekordbox dynamic grid, which carries a per-beat tempo but no
`tempo_changes` key, yields segments exactly like an own grid does: both are
read through their beat times.

THREE MEASUREMENTS, EACH BORROWED FROM A NAMED REFERENCE (mimicked, not
calibrated, per the hygiene rule):

- Changepoints: `detect_tempo_changes` (NATIVE-03), unchanged. Its longest
  segment names the nominal tempo and its marker count is `change_count`.
- Per-bar tempo: 60 * intervals / span over each 4-beat bar, median-smoothed
  over 3 bars. A bar further than `MIN_RELATIVE_BPM_DELTA` (the detector's own
  1 percent materiality floor) from nominal is off tempo.
- Local jitter: the inter-beat-interval coefficient of variation over an
  8-bar window (`MIN_SEGMENT_BARS`, the detector's own minimum section) after
  removing the window's linear trend, so a ramp is not mistaken for chaos.

KINDS, per bar then merged into runs:

- `unstable`: detrended jitter at or above `UNSTABLE_JITTER`.
- `ramp`: the smoothed tempo keeps moving the same way by at least
  `RAMP_MIN_PCT_PER_BAR` per bar for `RAMP_MIN_RUN_BARS` bars. A step moves
  for at most three bars after smoothing, so a held jump never reads as a ramp.
- `jump`: a held tempo at least 1 percent away from nominal.
- `steady`: none of the above.

VERDICT: `steady` (nothing moves and the grid is not variable_tempo),
`free_intro` (one ramp or unstable stretch opening the track, steady after),
`changes` (the changepoint detector confirms at least one change), else
`drift`.

A steady track returns NO segments, so the lane paints nothing on most of the
library. Any other verdict returns segments that tile the grid, steady tiles
included, so a reader can name the tempo of every stretch.

Requirements (mini-PRD):
  - A steady grid is verdict `steady` with zero segments.
    [if] a fixed 128 BPM click grid yields any segment [then] broken
  - An injected 3 percent ramp at 2:00 yields a ramp or jump starting within
    8 bars of 2:00, direction faster.
    [if] no non-steady segment starts within 8 bars of 2:00 [then] broken
  - A `variable_tempo` grid never reads `steady` (hard veto, #7879).
    [if] grid_class is variable_tempo and verdict is steady [then] broken
"""

from __future__ import annotations

import itertools
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from apps.analysis_beatgrid.grid_quality import (
    GridClass,
    classify_grid,
    grid_quality_message,
)
from apps.analysis_beatgrid.tempo_change import (
    BEATS_PER_BAR,
    MIN_RELATIVE_BPM_DELTA,
    MIN_SEGMENT_BARS,
    detect_tempo_changes,
)

# ----- Named constants ----------------------------------------------------

#: Bars in the jitter window: the detector's minimum section length.
JITTER_WINDOW_BARS = MIN_SEGMENT_BARS
#: Detrended inter-beat-interval coefficient of variation that reads as
#: unstable. Mimicked from the detector's own "realistic jitter" control
#: (10 ms beat sigma at 128 BPM, about 3 percent interval CV, which it must
#: call steady): anything at or below that is tracker wobble, not chaos.
UNSTABLE_JITTER = 0.04
#: A ramp moves at least the 1 percent materiality floor per section (8 bars).
RAMP_MIN_PCT_PER_BAR = MIN_RELATIVE_BPM_DELTA * 100 / MIN_SEGMENT_BARS
#: Bars a ramp must keep moving. A smoothed step moves for three at most
#: (two when it falls on a bar line, three when a bar straddles it).
RAMP_MIN_RUN_BARS = 4
#: Non-steady runs shorter than this are a fill or a wobble, not a stretch.
MIN_RUN_BARS = 2
#: Off-tempo percentage that maps to full severity: #7879's deep-red floor
#: (amber under 5 percent, red 5 to 15, deep red 15 and over).
SEVERE_PCT = 15.0
_SMOOTH_BARS = 3
#: Tempo ratios a beatgrid folds into without the music changing: half and
#: double time, and the 2/3, 3/4 triplet and dotted folds. Mimicked from the
#: deck's own BAR fold rule (AGENTS.md "/performance architecture": 0.5x and
#: 2x lock phase as the same pulse). A stretch within FOLD_TOLERANCE of one
#: of these, relative to nominal, is a grid artifact, drawn as `unstable`
#: and kept out of `largest_change_pct` and `change_count`. Found on the live
#: library Tue 3 Nov 2026: a 160 BPM track read 74.5, 240 and 318 BPM.
FOLD_RATIOS: tuple[float, ...] = (0.5, 2 / 3, 0.75, 4 / 3, 1.5, 2.0)
FOLD_TOLERANCE = 0.03

Kind = Literal["steady", "ramp", "jump", "unstable"]
Direction = Literal["faster", "slower", "none"]
Verdict = Literal["steady", "drift", "free_intro", "changes"]
VERDICTS: tuple[Verdict, ...] = ("steady", "drift", "free_intro", "changes")
KINDS: tuple[Kind, ...] = ("steady", "ramp", "jump", "unstable")


# ----- Results ------------------------------------------------------------


@dataclass(frozen=True)
class TempoSegment:
    start_s: float
    end_s: float
    #: Mean tempo over the stretch.
    bpm: float
    #: Smoothed tempo of its first and last bar (a ramp's two ends).
    bpm_start: float
    bpm_end: float
    #: `bpm` relative to nominal, percent, signed.
    delta_pct: float
    kind: Kind
    #: 0..1: furthest off-tempo percentage (or jitter percentage) / SEVERE_PCT.
    severity: float
    direction: Direction


@dataclass(frozen=True)
class TempoStability:
    nominal_bpm: float
    verdict: Verdict
    #: Furthest any stretch gets from nominal, percent, unsigned.
    largest_change_pct: float
    #: Changepoints the NATIVE-03 detector accepts on this grid.
    change_count: int
    grid_class: GridClass
    grid_quality_message: str
    segments: tuple[TempoSegment, ...]


@dataclass(frozen=True)
class _Bar:
    start_s: float
    end_s: float
    intervals: int
    bpm: float


# ----- Per-bar measurements -----------------------------------------------


def _bars(times: Sequence[float]) -> list[_Bar]:
    """4-beat bars from beat 0; a tail of one interval joins the last bar."""
    edges = [*range(0, len(times) - 1, BEATS_PER_BAR), len(times) - 1]
    if len(edges) > 2 and edges[-1] - edges[-2] < 2:
        del edges[-2]
    return [
        _Bar(times[a], times[b], b - a, 60.0 * (b - a) / (times[b] - times[a])) for a, b in itertools.pairwise(edges)
    ]


def _smoothed(values: Sequence[float]) -> list[float]:
    half = _SMOOTH_BARS // 2
    return [statistics.median(values[max(0, i - half) : i + half + 1]) for i in range(len(values))]


def _detrended_jitter(intervals: Sequence[float]) -> float:
    """CV of intervals around their own least-squares line."""
    count = len(intervals)
    if count < 3:
        return 0.0
    mean_x = (count - 1) / 2
    mean_y = statistics.fmean(intervals)
    sxx = sum((x - mean_x) ** 2 for x in range(count))
    slope = sum((x - mean_x) * (y - mean_y) for x, y in enumerate(intervals)) / sxx
    residuals = [y - (mean_y + slope * (x - mean_x)) for x, y in enumerate(intervals)]
    return statistics.pstdev(residuals) / mean_y


def _window_jitter(intervals: Sequence[float], bar_index: int) -> float:
    half = JITTER_WINDOW_BARS // 2
    lo = max(0, (bar_index - half) * BEATS_PER_BAR)
    hi = min(len(intervals), (bar_index + half) * BEATS_PER_BAR)
    return _detrended_jitter(intervals[lo:hi])


def _pct(bpm: float, nominal: float) -> float:
    return (bpm - nominal) / nominal * 100.0


def _is_fold(bpm: float, reference: float) -> bool:
    """True when `bpm` sits on a half / double / triplet fold of `reference`."""
    ratio = bpm / reference
    return any(abs(ratio / fold - 1.0) <= FOLD_TOLERANCE for fold in FOLD_RATIOS)


def _ramp_bars(smooth: Sequence[float], nominal: float) -> list[int]:
    """+1 / -1 for bars inside a sustained same-direction move, else 0."""
    slopes = [0] * len(smooth)
    for i in range(1, len(smooth) - 1):
        step_pct = _pct(smooth[i + 1], nominal) - _pct(smooth[i - 1], nominal)
        if abs(step_pct) / 2 >= RAMP_MIN_PCT_PER_BAR:
            slopes[i] = 1 if step_pct > 0 else -1
    marked = [0] * len(smooth)
    i = 0
    while i < len(slopes):
        j = i
        while j < len(slopes) and slopes[j] == slopes[i]:
            j += 1
        if slopes[i] != 0 and j - i >= RAMP_MIN_RUN_BARS:
            marked[i:j] = [slopes[i]] * (j - i)
        i = j
    return marked


def _bar_label(delta_pct: float, jitter: float, ramp: int, folded: bool) -> tuple[Kind, Direction]:
    if folded or jitter >= UNSTABLE_JITTER:
        return "unstable", "none"
    if ramp != 0:
        return "ramp", "faster" if ramp > 0 else "slower"
    if abs(delta_pct) >= MIN_RELATIVE_BPM_DELTA * 100:
        return "jump", "faster" if delta_pct > 0 else "slower"
    return "steady", "none"


# ----- Runs to segments ---------------------------------------------------


def _runs(labels: Sequence[tuple[Kind, Direction]]) -> list[tuple[int, int, Kind, Direction]]:
    runs: list[tuple[int, int, Kind, Direction]] = []
    for label, group in itertools.groupby(enumerate(labels), key=lambda item: item[1]):
        members = [index for index, _ in group]
        runs.append((members[0], members[-1] + 1, label[0], label[1]))
    return runs


def _absorb_short_runs(labels: list[tuple[Kind, Direction]]) -> list[tuple[Kind, Direction]]:
    """A non-steady run under MIN_RUN_BARS reads as steady (a fill, not a stretch)."""
    for lo, hi, kind, _direction in _runs(labels):
        if kind != "steady" and hi - lo < MIN_RUN_BARS:
            labels[lo:hi] = [("steady", "none")] * (hi - lo)
    return labels


def _segment(
    bars: Sequence[_Bar],
    smooth: Sequence[float],
    jitters: Sequence[float],
    run: tuple[int, int, Kind, Direction],
    nominal: float,
) -> TempoSegment:
    lo, hi, kind, direction = run
    start_s, end_s = bars[lo].start_s, bars[hi - 1].end_s
    bpm = 60.0 * sum(bar.intervals for bar in bars[lo:hi]) / (end_s - start_s)
    off_pct = max(abs(_pct(value, nominal)) for value in smooth[lo:hi])
    if kind == "unstable":
        # A folded or chaotic grid says nothing about how far the music moved,
        # so its severity is the jitter alone, never the fold's 50 or 100 percent.
        off_pct = max(jitters[lo:hi]) * 100
    elif kind == "steady":
        off_pct = 0.0
    return TempoSegment(
        start_s=round(start_s, 3),
        end_s=round(end_s, 3),
        bpm=round(bpm, 2),
        bpm_start=round(smooth[lo], 2),
        bpm_end=round(smooth[hi - 1], 2),
        delta_pct=round(_pct(bpm, nominal), 2),
        kind=kind,
        severity=round(min(1.0, off_pct / SEVERE_PCT), 3),
        direction=direction,
    )


def _verdict(segments: Sequence[TempoSegment], change_count: int, grid_class: GridClass) -> Verdict:
    moving = [segment for segment in segments if segment.kind != "steady"]
    if not moving and change_count == 0 and grid_class != "variable_tempo":
        return "steady"
    if (
        len(moving) == 1
        and moving[0].start_s == segments[0].start_s
        and moving[0].kind in ("unstable", "ramp")
        and len(segments) > 1
    ):
        return "free_intro"
    # `changes` is a detector-confirmed changepoint, so #7879's "N x change"
    # never reads "0 x change"; per-bar moves the detector does not confirm
    # are drift.
    if change_count > 0:
        return "changes"
    return "drift"


# ----- Entry point --------------------------------------------------------


def tempo_stability(beat_times_s: Sequence[float], beat_bpms: Sequence[float] | None = None) -> TempoStability:
    """Where the tempo of one served beatgrid moves, and how far.

    Needs at least two beats; the caller reports a grid without them as not
    analyzed rather than asking this function to invent an answer. Beat times
    must be strictly increasing (the detector raises otherwise).
    """
    if len(beat_times_s) < 2:
        raise ValueError(f"tempo stability needs at least 2 beats, got {len(beat_times_s)}")
    changes = detect_tempo_changes(beat_times_s)
    times = [float(t) for t in beat_times_s]
    *_, nominal = max(changes.segments, key=lambda seg: times[seg[1]] - times[seg[0]])
    quality = classify_grid(times, beat_bpms)

    intervals = [later - earlier for earlier, later in itertools.pairwise(times)]
    bars = _bars(times)
    smooth = _smoothed([bar.bpm for bar in bars])
    jitters = [_window_jitter(intervals, index) for index in range(len(bars))]
    ramps = _ramp_bars(smooth, nominal)
    # Raw bar tempi find the fold; the bars on either side of it straddle the
    # boundary and mix the two tempi into an in-between value, so they join it.
    raw_fold = [_is_fold(bar.bpm, nominal) or _is_fold(value, nominal) for bar, value in zip(bars, smooth, strict=True)]
    folded = [any(raw_fold[max(0, i - 1) : i + 2]) for i in range(len(bars))]
    labels = _absorb_short_runs(
        [_bar_label(_pct(smooth[i], nominal), jitters[i], ramps[i], folded[i]) for i in range(len(bars))]
    )
    segments = tuple(_segment(bars, smooth, jitters, run, nominal) for run in _runs(labels))
    change_count = sum(1 for marker in changes.markers if not _is_fold(marker.bpm_after, marker.bpm_before))
    verdict = _verdict(segments, change_count, quality.grid_class)
    largest = max(
        (abs(_pct(value, nominal)) for value, fold in zip(smooth, folded, strict=True) if not fold),
        default=0.0,
    )
    return TempoStability(
        nominal_bpm=round(nominal, 2),
        verdict=verdict,
        largest_change_pct=round(largest, 2),
        change_count=change_count,
        grid_class=quality.grid_class,
        grid_quality_message=grid_quality_message(quality),
        segments=() if verdict == "steady" else segments,
    )


__all__ = [
    "KINDS",
    "RAMP_MIN_PCT_PER_BAR",
    "SEVERE_PCT",
    "UNSTABLE_JITTER",
    "VERDICTS",
    "TempoSegment",
    "TempoStability",
    "tempo_stability",
]
