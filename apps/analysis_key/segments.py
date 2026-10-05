"""Key-change segmentation: bar-synchronous chroma, HMM/Viterbi over bars.

`specs/native-analysis-v1.md` section 5, "Key changes": "bar-synchronous chroma
on own downbeats, the EDM-profile posterior (the shipping key producer) as
emissions, HMM + Viterbi over bars, minimum 8-bar segments". No DJ tool
exposes segments, so there is no ground truth to fit against and the block is
validated as DISAGREEMENT data (Flow 8 Deck's 118 multi-segment tracks) plus
synthetic transposition injections, which is what
`tests/analysis_key/test_segments.py` does.

WHAT THIS MODULE IS NOT. It is not a second key estimator. The per-bar
emissions are the SHIPPING producer's own profile correlations
(`apps.analysis_key.profiles`), the same scorer the scalar key comes from, so a
segment label and the track's scalar key are answers from one instrument rather
than two that can disagree. The segment boundary is the only new decision here.

THE THREE DECISIONS, none of them silent:

**Downbeats are INPUT, not something this module finds.** Bars come from the
own beatgrid record's beats (`n == 1` positions). A track with fewer than two
own downbeats gets `status: missing` with `no_own_downbeats` -- not a fabricated
grid, and not `failed`, because nothing was measured and declined. The queue is
the fix, which is why `missing` is a state rather than a tooltip (spec section
3).

**A segment shorter than 8 bars is MERGED, never published.** A three-bar
excursion between two statements of the same key is a mis-label, not a key
change, and publishing it would put a marker on the deck. Merging is iterative
because absorbing one short run can shorten its neighbour.

**Under 8 bars there is no segment to publish at all** (`too_few_bars`), which
is `failed` with a reason rather than an `ok` block with an empty array: the
contract refuses that shape outright (spec section 3, the `beatgrid.beats`
empty-array rule's twin).

THE TWO HMM CONSTANTS ARE UNCALIBRATED PLACEHOLDERS, NOT MEASURED, and they
carry the same warning `apps/analysis_key/flags.py` carries on its thresholds:
no scored round has run against real modulations on this box (rekordbox truth
is absent -- see the key lane's round-0 log), so `EMISSION_GAIN` and
`SELF_TRANSITION_PROB` are principled starting points. A round that has real
annotated modulations replaces them; until then a boundary figure from this
module is a shape measurement, not a tuned one.

-Claude
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np

from apps.analysis_key import canon, profiles

#: Section 5's floor: a key change needs at least this many bars on each side.
MIN_SEGMENT_BARS = 8

#: No own downbeats to segment over. A STATE, not a failure: the key-change
#: analysis has not run, which `missing` says exactly.
REASON_NO_DOWNBEATS = "no_own_downbeats"

#: Fewer bars than one minimum-length segment fits in.
REASON_TOO_FEW_BARS = "too_few_bars"

#: See the module docstring: uncalibrated.
EMISSION_GAIN = 24.0
SELF_TRANSITION_PROB = 0.98

_N_KEYS = profiles.N_KEYS
_N_BARS_FOR_STABILITY = 64
_MIN_PROB = 1e-12

SegmentStatus = Literal["ok", "failed", "missing"]


#-----------------------------------------------------------------------------
# the bar grid
#-----------------------------------------------------------------------------

def _floor_5dp(seconds: float) -> float:
    """``seconds`` at the payload's 5 dp, never above it: the last segment
    ends at the record's own duration, and rounding that half-up publishes an
    end past ``duration_s`` that the write boundary refuses."""
    return math.floor(seconds * 100_000) / 100_000


@dataclass(frozen=True)
class BarGrid:
    """Bar extents in seconds: ``starts[i]`` to ``ends[i]`` is bar ``i``.

    ``start_bar``/``end_bar`` on a segment index THIS, not a beat array: the
    contract wants bar indices that are contiguous, and every bar here is one
    whole bar of the own grid.
    """

    starts: tuple[float, ...]
    ends: tuple[float, ...]

    def __post_init__(self) -> None:
        if len(self.starts) != len(self.ends):
            raise ValueError(
                f"bar grid has {len(self.starts)} starts and {len(self.ends)} "
                "ends; one bar has one extent"
            )
        for index, (start, end) in enumerate(zip(self.starts, self.ends, strict=True)):
            if not start < end:
                raise ValueError(
                    f"bar {index} runs from {start} to {end}; a bar has a "
                    "positive extent"
                )

    @property
    def n_bars(self) -> int:
        return len(self.starts)


def bar_grid_from_beats(
    beats: Sequence[Mapping[str, Any]], *, duration_s: float
) -> BarGrid | None:
    """Bars from the own beatgrid record's beats, or None without two downbeats.

    A downbeat is a beat the grid numbers ``n == 1``; the grid contract already
    enforces that numbering cycles 1..4, so the downbeats are the bar starts by
    construction. The final bar has no following downbeat to close it, so the
    record's own decoded length closes it -- checked, not assumed: a downbeat at
    or past `duration_s` would make a zero-length bar, so those are dropped
    (which is why this can return None for a grid that looked long enough).
    """
    downbeats = [
        float(beat["t"]) for beat in beats if int(beat["n"]) == 1 and float(beat["t"]) < duration_s
    ]
    if len(downbeats) < 2:
        return None
    starts = tuple(downbeats)
    ends = (*starts[1:], float(duration_s))
    return BarGrid(starts=starts, ends=ends)


#-----------------------------------------------------------------------------
# per-bar chroma
#-----------------------------------------------------------------------------

def bar_chroma(
    chroma: np.ndarray, times: Sequence[float], grid: BarGrid
) -> np.ndarray:
    """One chroma column per bar: the mean of the frames inside that bar.

    ``chroma`` is ``(12, n_frames)`` and ``times`` the corresponding frame
    times, so this stays a pure aggregation with no decoder in it (the decode
    belongs to the producer; the aggregation belongs here, where the bar
    hypothesis is).
    """
    if chroma.ndim != 2 or chroma.shape[0] != 12:
        raise ValueError(
            f"chroma must be a (12, n_frames) pitch-class matrix, got shape "
            f"{chroma.shape}"
        )
    if chroma.shape[1] != len(times):
        raise ValueError(
            f"chroma has {chroma.shape[1]} frames but {len(times)} frame times; "
            "a bar can only be averaged over frames it actually has"
        )
    frame_times = np.asarray(times, dtype=np.float64)
    columns = np.zeros((12, grid.n_bars))
    for index, (start, end) in enumerate(zip(grid.starts, grid.ends, strict=True)):
        inside = (frame_times >= start) & (frame_times < end)
        if not inside.any():
            # A bar narrower than one frame step. It has no chroma of its own,
            # and copying a neighbour's would be a fabricated measurement, so
            # the nearest frame is used and the caller can see the bar count
            # it produced. At CQT hop 512 / 44.1 kHz this needs a bar shorter
            # than ~12 ms, which the beatgrid contract's real bars are not.
            nearest = int(np.argmin(np.abs(frame_times - start)))
            inside = np.zeros_like(frame_times, dtype=bool)
            inside[nearest] = True
        columns[:, index] = chroma[:, inside].mean(axis=1)
    return columns


#-----------------------------------------------------------------------------
# emissions
#-----------------------------------------------------------------------------

def _log_emissions(correlations: np.ndarray) -> np.ndarray:
    """Per-bar log-likelihood over the 24 keys, from the shipping profiles.

    Correlations are in a narrow band (they are cosines of nonnegative
    vectors), so they are subtracted by their own bar maximum and scaled by
    :data:`EMISSION_GAIN` before the log-softmax: the scaling is what turns a
    difference between two nearly-identical candidates into a decision, and it
    is an uncalibrated placeholder (see the module docstring).
    """
    scaled = (correlations - correlations.max(axis=0, keepdims=True)) * EMISSION_GAIN
    peak = np.max(scaled, axis=0, keepdims=True)
    total = np.log(np.sum(np.exp(scaled - peak), axis=0, keepdims=True) + _MIN_PROB)
    return scaled - (peak + total)


#-----------------------------------------------------------------------------
# Viterbi
#-----------------------------------------------------------------------------

def _transition_log_probs() -> np.ndarray:
    stay = math.log(SELF_TRANSITION_PROB)
    move = math.log((1.0 - SELF_TRANSITION_PROB) / (_N_KEYS - 1))
    matrix = np.full((_N_KEYS, _N_KEYS), move)
    np.fill_diagonal(matrix, stay)
    return matrix


def _viterbi(log_emissions: np.ndarray) -> list[int]:
    """Most likely key per bar, under a stay-put-biased chain."""
    transition = _transition_log_probs()
    n_bars = log_emissions.shape[1]
    scores = log_emissions[:, 0].copy()
    backpointers: list[np.ndarray] = []
    for bar in range(1, n_bars):
        candidates = scores[:, None] + transition
        best_previous = np.argmax(candidates, axis=0)
        backpointers.append(best_previous)
        scores = candidates[best_previous, np.arange(_N_KEYS)] + log_emissions[:, bar]
    path = [int(np.argmax(scores))]
    for best_previous in reversed(backpointers):
        path.append(int(best_previous[path[-1]]))
    path.reverse()
    return path


#-----------------------------------------------------------------------------
# runs -> segments
#-----------------------------------------------------------------------------

@dataclass(frozen=True)
class _Run:
    key_index: int
    start_bar: int
    end_bar: int

    @property
    def width(self) -> int:
        return self.end_bar - self.start_bar


def _runs(path: Sequence[int]) -> list[_Run]:
    runs: list[_Run] = []
    for bar, key_index in enumerate(path):
        if runs and runs[-1].key_index == key_index:
            runs[-1] = _Run(key_index, runs[-1].start_bar, bar + 1)
        else:
            runs.append(_Run(key_index, bar, bar + 1))
    return runs


def _coalesce(runs: list[_Run]) -> list[_Run]:
    """Join adjacent runs that ended up naming the same key.

    Absorbing a short run into one neighbour can leave the two sides of it
    naming the same key -- the excursion is gone but the run list still carries
    a boundary -- and a boundary between two segments of one key is a marker
    the deck would paint for no reason.
    """
    coalesced: list[_Run] = []
    for run in runs:
        if coalesced and coalesced[-1].key_index == run.key_index:
            previous = coalesced[-1]
            coalesced[-1] = _Run(previous.key_index, previous.start_bar, run.end_bar)
            continue
        coalesced.append(run)
    return coalesced


def _merge_short_runs(runs: list[_Run], *, floor: int) -> list[_Run]:
    """Absorb every run shorter than `floor` into an adjacent one.

    The neighbour absorbed into is the WIDER one, and the previous run wins a
    tie: that keeps the merge deterministic rather than dependent on iteration
    order, and a tie is the only case where the choice is not obvious.
    """
    merged = list(runs)
    while len(merged) > 1:
        short = [index for index, run in enumerate(merged) if run.width < floor]
        if not short:
            break
        index = min(short, key=lambda i: (merged[i].width, i))
        if index == 0:
            target = 1
        elif index == len(merged) - 1:
            target = index - 1
        else:
            target = index - 1 if merged[index - 1].width >= merged[index + 1].width else index + 1
        first, second = sorted((index, target))
        neighbour = merged[target]
        merged[first:second + 1] = [
            _Run(
                key_index=neighbour.key_index,
                start_bar=merged[first].start_bar,
                end_bar=merged[second].end_bar,
            )
        ]
    return _coalesce(merged)


#-----------------------------------------------------------------------------
# the block
#-----------------------------------------------------------------------------

@dataclass(frozen=True)
class KeySegment:
    start_bar: int
    end_bar: int
    start_s: float
    end_s: float
    key: canon.Key
    confidence: float


@dataclass(frozen=True)
class SegmentBlock:
    """The `segments` block of the key lane payload.

    `status` follows the lane's own vocabulary: `ok` with one segment is a
    stable key, `ok` with two or more is a real change, `missing` is "not
    analyzed yet", `failed` names why a measurement could not be published.
    """

    status: SegmentStatus
    reason: str | None
    segments: tuple[KeySegment, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "segments": [
                {
                    "start_bar": segment.start_bar,
                    "end_bar": segment.end_bar,
                    "start_s": round(segment.start_s, 5),
                    # Interior ends round like the next start so the two
                    # stay equal; only the last end meets duration_s.
                    "end_s": (
                        _floor_5dp(segment.end_s) if index == len(self.segments) - 1
                        else round(segment.end_s, 5)
                    ),
                    "key_camelot": canon.to_camelot(segment.key),
                    "key_openkey": canon.to_open_key(segment.key),
                    "confidence": segment.confidence,
                }
                for index, segment in enumerate(self.segments)
            ],
        }


def missing_block(reason: str = REASON_NO_DOWNBEATS) -> SegmentBlock:
    return SegmentBlock(status="missing", reason=reason, segments=())


def _failed_block(reason: str) -> SegmentBlock:
    return SegmentBlock(status="failed", reason=reason, segments=())


def segment_bars(
    chroma: np.ndarray, grid: BarGrid, *, duration_s: float
) -> SegmentBlock:
    """One key segment per stable stretch of bars, from per-bar chroma.

    `chroma` holds ONE COLUMN PER BAR (`bar_chroma`'s output), not frames: the
    bar IS the unit of this analysis, and accepting frames here would invite a
    caller to segment at a resolution the minimum-length rule is defined
    against.
    """
    if grid.ends and grid.ends[-1] > duration_s + 1e-9:
        raise ValueError(
            f"bar grid ends at {grid.ends[-1]}s but duration_s is {duration_s}s"
        )
    if chroma.ndim != 2 or chroma.shape[0] != 12:
        raise ValueError(
            f"chroma must be a (12, n_bars) pitch-class matrix, got shape {chroma.shape}"
        )
    if chroma.shape[1] != grid.n_bars:
        raise ValueError(
            f"chroma has {chroma.shape[1]} bars but the grid has {grid.n_bars}; "
            "a segment boundary is a bar boundary"
        )
    if grid.n_bars < MIN_SEGMENT_BARS:
        return _failed_block(REASON_TOO_FEW_BARS)

    correlations = profiles.correlation_matrix(chroma)
    runs = _merge_short_runs(
        _runs(_viterbi(_log_emissions(correlations))), floor=MIN_SEGMENT_BARS
    )
    segments = tuple(_segment_from_run(run, correlations, grid) for run in runs)
    return SegmentBlock(status="ok", reason=None, segments=segments)


def _segment_from_run(run: _Run, correlations: np.ndarray, grid: BarGrid) -> KeySegment:
    """One run into one segment, with the confidence the profile actually gave.

    Confidence is the mean CORRELATION of the winning key over the run's bars,
    read from the correlation matrix rather than back out of the Viterbi score:
    that score has accumulated the transition prior, so a long stable run would
    report near-certainty purely for being long, which is exactly the kind of
    number this lane is not allowed to publish as a measurement.
    """
    confidence = float(correlations[run.key_index, run.start_bar:run.end_bar].mean())
    return KeySegment(
        start_bar=run.start_bar,
        end_bar=run.end_bar,
        start_s=grid.starts[run.start_bar],
        end_s=grid.ends[run.end_bar - 1],
        key=profiles.key_for_index(run.key_index),
        confidence=min(max(confidence, 0.0), 1.0),
    )


def segment_audio(
    chroma: np.ndarray,
    times: Iterable[float],
    beats: Sequence[Mapping[str, Any]],
    *,
    duration_s: float,
) -> SegmentBlock:
    """`bar_grid_from_beats` -> `bar_chroma` -> `segment_bars`, in one call.

    The whole read of a beatgrid record goes through here, so a caller cannot
    build a grid and then segment a different one.
    """
    grid = bar_grid_from_beats(beats, duration_s=duration_s)
    if grid is None:
        return missing_block()
    return segment_bars(
        bar_chroma(chroma, list(times), grid), grid, duration_s=duration_s
    )


__all__ = [
    "EMISSION_GAIN",
    "MIN_SEGMENT_BARS",
    "REASON_NO_DOWNBEATS",
    "REASON_TOO_FEW_BARS",
    "SELF_TRANSITION_PROB",
    "BarGrid",
    "KeySegment",
    "SegmentBlock",
    "bar_chroma",
    "bar_grid_from_beats",
    "missing_block",
    "segment_audio",
    "segment_bars",
]
