"""Tempo-change tags: changepoint detection over inter-beat intervals.

v1 IBI tags (``TempoMarker``, ``static_grid_untrusted``) live here. v2 multi-
anchor tempo maps live in ``tempo_map.py`` and must not be merged into
``detect_tempo_changes``.

WHAT THIS ANSWERS. A single global BPM cannot describe every track with
dynamic tempo. Quantize, Beat Sync and beat loops can drift when consumers
treat such a track as fixed-tempo. This module's job is to say WHEN one number is
not enough, and to hand the caller the marker positions so the payload can set
`beatgrid.static_grid_untrusted` and `performance_hints.dynamic_tempo`
(specs/native-analysis-v1.md section 2).

WHY CHANGEPOINTS OVER INTER-BEAT INTERVALS AND NOT A TEMPOGRAM. The beats are
already tracked. An inter-beat interval series IS the tempo curve, sampled once
per beat, so the question reduces to the classic change-in-mean segmentation
problem and needs no second pass over audio. The research survey looked for a
published algorithm for fitting a minimal set of piecewise-constant tempo
anchors to a beat sequence and did not find one
(docs/research/beatgrid-and-segmentation-sota-20260906-a-beat-tracking.md
section 2.4); it is a segmented-regression problem, not a research risk.

TWO GATES, BOTH REQUIRED, AND THEY FAIL IN DIFFERENT DIRECTIONS. A split is
accepted only when it is BOTH statistically supported (a BIC gain over the
one-segment fit, which asks "is this step bigger than this track's own beat
jitter") AND musically material (a relative tempo delta above the floor, which
asks "would a DJ's grid actually drift"). The statistical gate alone fires on a
0.02 BPM step in an extremely quiet, extremely regular track; the delta gate
alone fires on jitter in a loose live recording. Requiring both guards against accepting tracker jitter as a tempo change.

MINIMUM SEGMENT LENGTH IS EIGHT BARS, which is 32 beats in 4/4. Shorter than
that and a changepoint is not describing a tempo section, it is describing a
fill, a dropped beat, or a tracker wobble. It also bounds the marker count: a
six-minute track cannot produce more than a couple of dozen markers however
noisy its beats are.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass

# ----- Named constants ----------------------------------------------------

BEATS_PER_BAR = 4
MIN_SEGMENT_BARS = 8
MIN_SEGMENT_BEATS = MIN_SEGMENT_BARS * BEATS_PER_BAR  # 32

# Musical materiality gate. 1 percent of tempo is roughly 1.3 BPM at 128, which
# is a quarter beat of drift over 32 bars: audible, and past any beat tracker's
# own jitter on a fixed grid.
MIN_RELATIVE_BPM_DELTA = 0.01

# Statistical gate. A change-in-mean split spends one extra mean and one extra
# variance, so the Schwarz penalty is 2 * ln(n). The multiplier is the safety
# margin over that: measured on the fixed-tempo partition, this is the knob
# that trades detection rate against false positives, and it is ratcheted per
# round in the experiment log rather than tuned per track.
BIC_PENALTY_MULTIPLIER = 2.0

# Guard against log(0) on a perfectly synthetic series with zero variance.
_VARIANCE_FLOOR = 1e-12


# ----- Results ------------------------------------------------------------


@dataclass(frozen=True)
class TempoMarker:
    """One tempo change, in the shape the `/anlz` payload carries it."""

    at_s: float
    bpm_before: float
    bpm_after: float
    confidence: float


@dataclass(frozen=True)
class TempoAnalysis:
    static_grid_untrusted: bool
    markers: tuple[TempoMarker, ...]
    n_beats: int
    # Segment boundaries as beat indices, inclusive start and exclusive end.
    segments: tuple[tuple[int, int, float], ...]


# ----- Cost model ---------------------------------------------------------


def _segment_cost(intervals: Sequence[float], start: int, end: int) -> float:
    """Gaussian negative log-likelihood of a constant-mean segment.

    `n * ln(variance)` up to constants that cancel when costs are differenced,
    which is the only way this value is ever used.
    """
    n = end - start
    if n <= 0:
        return 0.0
    window = intervals[start:end]
    mean = sum(window) / n
    variance = sum((x - mean) ** 2 for x in window) / n
    return n * math.log(max(variance, _VARIANCE_FLOOR))


def _mean_bpm(intervals: Sequence[float], start: int, end: int) -> float:
    window = intervals[start:end]
    mean_interval = sum(window) / len(window)
    return 60.0 / mean_interval if mean_interval > 0 else 0.0


def _confidence(gain: float, penalty: float) -> float:
    """Monotone rank score of how far the BIC gain clears its threshold.

    NOT a probability. `gain == penalty` reads 0, twice the penalty reads 0.63,
    and it saturates towards 1. It exists so a reader can rank markers, and it
    is named `confidence` because that is the field name the spec fixes; the
    docstring is the only place its meaning lives, so do not paraphrase it.
    """
    if penalty <= 0:
        return 0.0
    return round(1.0 - math.exp(-max(0.0, gain - penalty) / penalty), 4)


# ----- Binary segmentation ------------------------------------------------


def _is_material(intervals: Sequence[float], start: int, split: int, end: int) -> bool:
    """True when the two segments differ in mean tempo by at least the floor."""
    bpm_before = _mean_bpm(intervals, start, split)
    bpm_after = _mean_bpm(intervals, split, end)
    if bpm_before <= 0:
        return False
    return abs(bpm_after - bpm_before) / bpm_before >= MIN_RELATIVE_BPM_DELTA


def _accepted_split(
    intervals: Sequence[float], start: int, end: int
) -> tuple[int, float] | None:
    """The best split among those clearing BOTH gates, or None.

    BOTH GATES ARE APPLIED PER CANDIDATE, NOT TO THE WINNER ONLY. This used to
    pick the single highest-BIC-gain split and then test it for materiality,
    which discards the whole track the moment the strongest statistical split is
    tracker jitter: a real tempo change sitting at a slightly lower gain was
    never examined, and the track was published `static_grid_untrusted=False`
    despite a material change being present and findable (Codex P1 BLOCKING on
    PR #1514). A gate that rejects the argmax must not also reject the search.

    Only positions leaving at least MIN_SEGMENT_BEATS on both sides are
    considered, so the minimum-segment rule is enforced by construction rather
    than filtered afterwards.
    """
    n = end - start
    if n < 2 * MIN_SEGMENT_BEATS:
        return None

    penalty = BIC_PENALTY_MULTIPLIER * math.log(n)
    whole = _segment_cost(intervals, start, end)
    best: tuple[int, float] | None = None
    for split in range(start + MIN_SEGMENT_BEATS, end - MIN_SEGMENT_BEATS + 1):
        gain = whole - _segment_cost(intervals, start, split) - _segment_cost(
            intervals, split, end
        )
        if gain <= penalty:
            continue
        if not _is_material(intervals, start, split, end):
            continue
        if best is None or gain > best[1]:
            best = (split, gain)
    return None if best is None else (best[0], _confidence(best[1], penalty))


def _validated(beat_times: Sequence[float]) -> list[float]:
    """Beat times as floats, or raise. A non-monotonic series is a bug upstream.

    Raising beats returning a plausible segmentation of nonsense: the caller
    would have no way to tell the two apart.
    """
    beats = [float(t) for t in beat_times]
    for earlier, later in itertools.pairwise(beats):
        if later <= earlier:
            raise ValueError(
                f"beat times must be strictly increasing: {later} follows {earlier}"
            )
    return beats


def _single_segment(beats: list[float], intervals: list[float]) -> TempoAnalysis:
    """A track too short to hold two minimum segments is one segment, never zero."""
    bpm = _mean_bpm(intervals, 0, len(intervals)) if intervals else 0.0
    return TempoAnalysis(
        static_grid_untrusted=False,
        markers=(),
        n_beats=len(beats),
        segments=((0, len(intervals), bpm),) if intervals else (),
    )


def _segments_for(intervals: Sequence[float], boundaries: list[tuple[int, float]]) -> list[tuple]:
    """`(lo, hi, mean bpm)` for the sections these boundaries cut."""
    edges = [0, *(b for b, _ in boundaries), len(intervals)]
    return [(lo, hi, _mean_bpm(intervals, lo, hi)) for lo, hi in itertools.pairwise(edges)]


def _final_gates(
    intervals: Sequence[float], segments: list[tuple], index: int
) -> tuple[bool, float]:
    """`(clears both gates, confidence)` for one boundary against its FINAL pair.

    BOTH gates are recomputed, not just the tempo one. The BIC gain and its
    penalty are re-derived over the union of the two sections the boundary
    finally sits between, because a gain earned against a broad ancestor
    segment can fail against the narrow pair that survives refinement, and the
    confidence carried forward from the ancestor then describes a split the
    final data no longer supports (Codex P1 BLOCKING on PR #1514: gain 5.09
    against penalty 8.76, published at confidence 0.9987).
    """
    lo, _, bpm_before = segments[index]
    split = segments[index][1]
    hi, bpm_after = segments[index + 1][1], segments[index + 1][2]
    if bpm_before <= 0 or hi - lo < 2:
        return False, 0.0
    if abs(bpm_after - bpm_before) / bpm_before < MIN_RELATIVE_BPM_DELTA:
        return False, 0.0

    penalty = BIC_PENALTY_MULTIPLIER * math.log(hi - lo)
    gain = (
        _segment_cost(intervals, lo, hi)
        - _segment_cost(intervals, lo, split)
        - _segment_cost(intervals, split, hi)
    )
    if gain <= penalty:
        return False, 0.0
    return True, _confidence(gain, penalty)


def _revalidated(
    intervals: Sequence[float], boundaries: list[tuple[int, float]]
) -> list[tuple[int, float]]:
    """Re-test every boundary against its FINAL neighbours, and restate confidence.

    Both gates are applied while splitting, against the segment being split.
    Recursion then subdivides those segments, so the two sections a surviving
    boundary finally sits between are NARROWER than the ones it was accepted
    against: the mean tempos can differ by less than the floor, AND the BIC gain
    can fall below its penalty. Either way the detector would publish a marker
    its own rules no longer support, carrying a confidence computed from an
    ancestor segment (Codex P1 BLOCKING on PR #1514, twice).

    One boundary is dropped per pass, the least confident offender first,
    because removing a boundary MERGES its two sections and changes what its
    neighbours are compared against; a neighbour can become valid or invalid as
    a result. Each pass strictly shortens the list, so this terminates. The
    confidence of every survivor is RESTATED from its final gain, so the number
    published describes the split that was actually kept.
    """
    while True:
        segments = _segments_for(intervals, boundaries)
        verdicts = [_final_gates(intervals, segments, i) for i in range(len(boundaries))]
        offenders = [i for i, (ok, _) in enumerate(verdicts) if not ok]
        if not offenders:
            return [
                (split, verdicts[i][1]) for i, (split, _) in enumerate(boundaries)
            ]
        boundaries.pop(min(offenders, key=lambda i: boundaries[i][1]))


def detect_tempo_changes(beat_times: Sequence[float]) -> TempoAnalysis:
    """Segment one track's beats into piecewise-constant tempo sections.

    Beat times must be strictly increasing; a non-monotonic series is a bug in
    whatever produced it and raises here rather than yielding a plausible
    segmentation of nonsense.
    """
    beats = _validated(beat_times)
    intervals = [later - earlier for earlier, later in itertools.pairwise(beats)]
    if len(intervals) < 2 * MIN_SEGMENT_BEATS:
        return _single_segment(beats, intervals)

    boundaries: list[tuple[int, float]] = []
    pending: list[tuple[int, int]] = [(0, len(intervals))]
    while pending:
        start, end = pending.pop()
        found = _accepted_split(intervals, start, end)
        if found is None:
            continue
        split, confidence = found
        boundaries.append((split, confidence))
        pending.append((start, split))
        pending.append((split, end))

    boundaries.sort()
    boundaries = _revalidated(intervals, boundaries)
    segments = tuple(
        (lo, hi, round(bpm, 4)) for lo, hi, bpm in _segments_for(intervals, boundaries)
    )

    markers = tuple(
        TempoMarker(
            # A changepoint at interval index `split` means the interval from
            # beat `split` to beat `split + 1` is the first of the new tempo,
            # so the change is heard at beat `split`.
            at_s=round(beats[split], 4),
            bpm_before=segments[i][2],
            bpm_after=segments[i + 1][2],
            confidence=confidence,
        )
        for i, (split, confidence) in enumerate(boundaries)
    )
    return TempoAnalysis(
        static_grid_untrusted=bool(markers),
        markers=markers,
        n_beats=len(beats),
        segments=segments,
    )
