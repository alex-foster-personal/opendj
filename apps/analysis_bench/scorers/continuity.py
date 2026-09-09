"""Continuity metrics: CMLc, CMLt, AMLc and AMLt.

SPLIT OUT OF `scorer.py`, and not only for file size. These are a distinct
metric FAMILY with a distinct validation story: the rest of the scorer is
pinned by synthetic acceptance tests alone, while these are additionally
cross-checked against a reference implementation on real data by
`scripts/beatbench/verify_continuity.py`. That control caught a real defect here
on Tue 8 Sep 2026 (the wrong denominator, see `_continuity_pair`), which is a
good reason for the code it guards to be findable in one place.

`scorer.py` re-exports everything below, so `SCORER_VERSION` still stamps one
ruler and no caller has to know about the split.

WHY THESE JOIN F-MEASURE IN v1.1.0 AND WHY F ALONE WAS NOT ENOUGH. F at a
fixed +/- 70 ms counts how many beats landed close to a reference beat. It has
no opinion about whether they landed CONSECUTIVELY, and none about whether the
candidate's own spacing tracks the reference's. A tracker that hits every other
beat perfectly and free-runs in between can post a respectable F while being
unusable as a grid, which is exactly the failure mode round 0 could see in
`beat_count_ratio` but could not score.

The continuity metrics answer the grid question directly. A beat is
"continuously correct" only when BOTH its phase and its period agree with the
reference to within 17.5 percent of the local inter-beat interval, so a beat in
the right place at the wrong tempo does not count. CMLc is the longest unbroken
run of such beats; CMLt is their total, across all runs. The `t` variants are
the ones quoted because a grid that recovers after a fill is more useful than
one that never breaks and stops early.

AML ("allowed metrical levels") takes the best score over the reference AND four
metrical variations of it: double tempo, the off-beat, and the two half-tempo
phases. That is what separates "found the wrong pulse" from "found the pulse,
chose a different metrical level" without hand-waving: a clean half-tempo
tracker reads CMLt near zero and AMLt near one.

THE GAP IS NOT AN OCTAVE-ERROR RATE, and this docstring used to say it was.
Both terms are fractional CONTINUITY scores rather than counts of tracks, so
their difference is not a rate over anything; and one of the four variations is
the OFF-BEAT, which is a phase relationship and not a metrical level at all, so
a nonzero gap can be produced with no octave error present. Read AMLt minus
CMLt as continuity recovered by the allowed variants, an upper bound on
level-and-phase disagreement. The classified octave rate is the half and double
columns, which are counted per track (Codex P2 on PR #1514).

INDEPENDENT REIMPLEMENTATION, STDLIB, following the definitions mir_eval
implements (mir_eval.beat.continuity, ISC licence). This module cannot import
mir_eval: it is imported by pytest in the repo venv while the analyzers live in
throwaway PEP 723 environments, and the whole point of the scorer is that it
carries no dependency that could differ between rounds.
"""

from __future__ import annotations

import bisect
import itertools
from collections.abc import Sequence
from dataclasses import dataclass

# Continuity tolerances from the MIR literature (Hainsworth / Davies), the same
# defaults mir_eval uses: a beat is continuously correct when it sits within
# 17.5 percent of the reference inter-beat interval of its nearest reference
# beat AND its own interval matches that reference interval to within 17.5
# percent. Relative on purpose: at 70 BPM a 70 ms window is a tenth of a beat,
# at 180 BPM it is a third.
CONTINUITY_PHASE_TOLERANCE = 0.175
CONTINUITY_PERIOD_TOLERANCE = 0.175

# Below this a continuity figure is describing two or three beats and is noise.
MIN_BEATS_FOR_CONTINUITY = 4


@dataclass(frozen=True)
class ContinuityScore:
    """The four continuity figures, or None when there is too little to score."""

    cmlc: float | None
    cmlt: float | None
    amlc: float | None
    amlt: float | None
    n_reference: int
    n_candidate: int


def _interpolate_midpoints(beats: Sequence[float]) -> list[float]:
    """Double the metrical level by inserting the midpoint of every interval."""
    doubled: list[float] = []
    for earlier, later in itertools.pairwise(beats):
        doubled.append(earlier)
        doubled.append((earlier + later) / 2.0)
    if beats:
        doubled.append(beats[-1])
    return doubled


def reference_variations(beats: Sequence[float]) -> list[list[float]]:
    """The reference plus the four metrical levels AML is allowed to prefer.

    Order is fixed and the original comes first, so a tie can never relabel a
    correct answer as a metrical variation of itself.
    """
    original = [float(t) for t in beats]
    doubled = _interpolate_midpoints(original)
    return [
        original,
        doubled,               # twice the metrical level
        doubled[1::2],         # the off-beat
        original[::2],         # half tempo, odd phase
        original[1::2],        # half tempo, even phase
    ]


def _nearest_index(cand: list[float], target: float) -> int:
    """Index of the candidate beat closest to `target`.

    Binary search rather than a linear scan: both series are sorted, and the
    quadratic form made a full four-candidate rescore over 337 fixtures take
    minutes instead of seconds.
    """
    pos = bisect.bisect_left(cand, target)
    if pos >= len(cand):
        return len(cand) - 1
    if pos > 0 and abs(cand[pos - 1] - target) <= abs(cand[pos] - target):
        return pos - 1
    return pos


def _beat_is_correct(
    ref: list[float],
    cand: list[float],
    i: int,
    phase_tolerance: float,
    period_tolerance: float,
) -> bool:
    """Is reference beat `i` continuously correct? BOTH phase AND period must agree.

    Requiring the period too is what stops a beat that merely lands in the right
    PLACE, at the wrong tempo, from counting. That is the whole difference
    between continuity and a plain proximity F-measure.
    """
    nearest = _nearest_index(cand, ref[i])
    deviation = cand[nearest] - ref[i]
    ref_interval = ref[1] - ref[0] if i == 0 else ref[i] - ref[i - 1]
    cand_interval = cand[1] - cand[0] if nearest == 0 else cand[nearest] - cand[nearest - 1]
    if ref_interval <= 0 or cand_interval <= 0:
        return False
    return (
        abs(deviation) < phase_tolerance * ref_interval
        and abs(cand_interval - ref_interval) < period_tolerance * ref_interval
    )


def _continuity_pair(
    reference: Sequence[float],
    candidate: Sequence[float],
    phase_tolerance: float,
    period_tolerance: float,
) -> tuple[float, float]:
    """`(longest-run fraction, total fraction)` of continuously correct beats."""
    ref = list(reference)
    cand = list(candidate)
    if len(ref) < 2 or len(cand) < 2:
        return 0.0, 0.0

    correct = [
        _beat_is_correct(ref, cand, i, phase_tolerance, period_tolerance)
        for i in range(len(ref))
    ]

    total = sum(correct)
    longest = current = 0
    for hit in correct:
        current = current + 1 if hit else 0
        longest = max(longest, current)

    # DENOMINATOR IS max(n_reference, n_candidate), NOT n_reference, and the
    # difference is the whole point. Dividing by the reference alone lets a
    # candidate that emits MORE beats than the reference collect its hits for
    # free: every extra beat is invisible to a per-reference-beat loop, so beat
    # spam raises the score. mir_eval normalizes this way for that reason, and
    # `scripts/beatbench/verify_continuity.py` caught the per-reference version
    # here on Tue 8 Sep 2026 against the constant-128 control, which emits 87
    # beats where the reference has 83 to 86 (same numerators, denominators
    # 83/84/86 against mir_eval's 87). Do not "simplify" this back.
    denominator = max(len(ref), len(cand))
    return longest / denominator, total / denominator


def score_continuity(
    reference: Sequence[float],
    candidate: Sequence[float],
    phase_tolerance: float = CONTINUITY_PHASE_TOLERANCE,
    period_tolerance: float = CONTINUITY_PERIOD_TOLERANCE,
) -> ContinuityScore:
    """CMLc, CMLt, AMLc and AMLt for one candidate against one reference window.

    Raises on an empty reference for the same reason `score_positions` does: a
    track with no rekordbox grid has no ground truth, and scoring it would
    invent one. Too FEW beats on either side returns None rather than 0.0,
    because an unmeasurable case must not render as a failing one.
    """
    ref = sorted(float(t) for t in reference)
    if not ref:
        raise ValueError("no reference beats: this track has no rekordbox grid to score against")
    cand = sorted(float(t) for t in candidate)

    if len(ref) < MIN_BEATS_FOR_CONTINUITY or len(cand) < MIN_BEATS_FOR_CONTINUITY:
        return ContinuityScore(None, None, None, None, len(ref), len(cand))

    scored = [
        _continuity_pair(variation, cand, phase_tolerance, period_tolerance)
        for variation in reference_variations(ref)
        if len(variation) >= 2
    ]
    cmlc, cmlt = scored[0]
    amlc = max(c for c, _ in scored)
    amlt = max(t for _, t in scored)
    return ContinuityScore(cmlc, cmlt, amlc, amlt, len(ref), len(cand))
