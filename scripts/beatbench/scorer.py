"""The beat-mapping scorer: one implementation, versioned, reused every round.

WHY THIS IS A SEPARATE MODULE AND NOT PART OF EACH RUNNER. The point of a
benchmark is comparing rounds, and two rounds are only comparable if the ruler
did not move between them. If each analyzer scored itself, a change in one
runner's notion of "close enough" would show up as a candidate improving. So
the candidates emit raw beat times and nothing else, and every number in every
table comes from here. SCORER_VERSION is stamped into each artifact, and a
change to what any metric MEANS must bump it, so a later reader can tell a
rescoring apart from a re-measurement.

PURE STDLIB ON PURPOSE. This module is imported by pytest inside the repo venv,
while the analyzers live in throwaway PEP 723 environments carrying torch and
friends. Keeping the scorer dependency-free is what lets the acceptance tests
run in the normal test suite instead of behind a heavy optional marker.

THE THREE METRICS, AND WHY EACH IS SHAPED THE WAY IT IS.

BPM agreement is reported at three named tolerances rather than one. 0.01 BPM
is not an arbitrary tight band: rekordbox stores tempo as an integer of BPM
x100, so 0.01 IS the storage granularity, and matching there means the analyzer
landed on literally the same stored value. 0.1 BPM is the band where a blend
holds: at 128 BPM a 0.1 error accumulates roughly 190 ms of drift over four
minutes, survivable in a short transition. 1.0 BPM is the "same tempo reading,
would need nudging" band; beyond it the analyzer disagreed about the tempo.
Octave errors (half, double, and the 3:2 and 2:3 relatives) are classified
SEPARATELY rather than counted as plain misses, because "found the pulse, chose
the wrong metrical level" is a different and far more recoverable failure than
"found the wrong pulse".

Beat position is reported in two variants because a single number cannot
separate the two ways a grid can be wrong. RAW nearest-beat error carries phase
offset and jitter together. Removing the best global shift (the median signed
error, which is robust to the aliasing that a half-tempo candidate produces)
leaves only jitter. A candidate with a large raw p50 and a tiny shifted p50 has
a correct grid in the wrong place, which a single constant can fix. A candidate
with both large has an unstable grid, which cannot be fixed by shifting.
Alongside those, an F-measure at +/-70 ms is the standard beat-tracking
agreement figure from the MIR literature, computed with one-to-one matching so
an analyzer cannot inflate recall by emitting duplicate beats.

Downbeat agreement is the fraction of rekordbox bar-1 beats that a candidate
downbeat lands on. Analyzers that do not emit downbeats report N/A, never 0.0.
Scoring silence as failure would make a librosa-style tracker look worse than
it is, and the honest denominators rule applies to capability as much as to
data.
"""

from __future__ import annotations

import bisect
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

# ----- Version and named constants ---------------------------------------

# Bump on any change to what a metric MEANS, never on a refactor.
SCORER_VERSION = "1.0.0"

# The MIR-standard beat-tracking tolerance. Used for both the F-measure and
# downbeat agreement so the two figures are on the same footing.
BEAT_TOLERANCE_S = 0.070

# rekordbox stores tempo as int(BPM x100), so this is the storage granularity.
BPM_EXACT_TOL = 0.01
BPM_TIGHT_TOL = 0.1
BPM_LOOSE_TOL = 1.0

# Float slack so a value sitting exactly on a boundary does not fail by 1 ulp.
_EPS = 1e-9

# Metrical relations worth naming, checked in this order. "same" must come
# first so a correct answer is never relabelled as an octave error.
TEMPO_RELATIONS: tuple[tuple[str, float], ...] = (
    ("same", 1.0),
    ("half", 0.5),
    ("double", 2.0),
    ("two_thirds", 2.0 / 3.0),
    ("three_halves", 1.5),
    ("third", 1.0 / 3.0),
    ("triple", 3.0),
)

# Octave bands are relative, not absolute: 1 BPM of slack is generous at 60 BPM
# and far too tight at 200, so the band scales with the target multiple.
TEMPO_RELATION_REL_TOL = 0.02


# ----- Small numeric helpers ---------------------------------------------


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolation percentile, or None when there is no data.

    Returning None rather than 0.0 for an empty series is deliberate: a missing
    measurement must never render as a perfect score.
    """
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    pos = (q / 100.0) * (len(ordered) - 1)
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    frac = pos - low
    return float(ordered[low] + (ordered[high] - ordered[low]) * frac)


def _median(values: Sequence[float]) -> float | None:
    return percentile(values, 50)


def window_slice(times: Iterable[float], start_s: float, end_s: float) -> list[float]:
    """Half-open [start, end) window, so adjacent windows never double count."""
    return [float(t) for t in times if start_s <= t < end_s]


# ----- Grid shape helpers -------------------------------------------------


def grid_is_dynamic(beats: Sequence[dict[str, Any]]) -> bool:
    """True when a rekordbox grid carries more than one tempo.

    Dynamic grids are the population that a fixed-BPM analyzer silently
    flatters itself on, so this predicate drives the fixed-vs-dynamic split
    that every table in the report is required to show.
    """
    seen: list[float] = []
    for beat in beats:
        bpm = round(float(beat["bpm"]), 2)
        if not any(abs(bpm - s) < BPM_EXACT_TOL for s in seen):
            seen.append(bpm)
            if len(seen) > 1:
                return True
    return False


def downbeat_times(beats: Sequence[dict[str, Any]]) -> list[float]:
    """rekordbox bar-1 beats. PQTZ numbers beats within the bar, 1 through 4."""
    return [float(b["t"]) for b in beats if int(b.get("n", 0)) == 1]


# ----- BPM scoring --------------------------------------------------------


def classify_tempo_relation(
    reference_bpm: float, candidate_bpm: float, rel_tol: float = TEMPO_RELATION_REL_TOL
) -> str:
    """Name the metrical relation between two tempi, or 'none'."""
    if reference_bpm <= 0 or candidate_bpm <= 0:
        return "none"
    ratio = candidate_bpm / reference_bpm
    for name, target in TEMPO_RELATIONS:
        if abs(ratio - target) <= rel_tol * target:
            return name
    return "none"


@dataclass(frozen=True)
class BpmScore:
    reference_bpm: float
    candidate_bpm: float | None
    abs_error: float | None
    within_0_01: bool
    within_0_1: bool
    within_1_0: bool
    relation: str

    @property
    def is_octave_error(self) -> bool:
        return self.relation not in ("same", "none", "absent")


def score_bpm(reference_bpm: float, candidate_bpm: float | None) -> BpmScore:
    """Compare one candidate tempo against rekordbox's stored tempo."""
    if candidate_bpm is None:
        return BpmScore(reference_bpm, None, None, False, False, False, "absent")
    error = abs(float(candidate_bpm) - float(reference_bpm))
    return BpmScore(
        reference_bpm=float(reference_bpm),
        candidate_bpm=float(candidate_bpm),
        abs_error=error,
        within_0_01=error <= BPM_EXACT_TOL + _EPS,
        within_0_1=error <= BPM_TIGHT_TOL + _EPS,
        within_1_0=error <= BPM_LOOSE_TOL + _EPS,
        relation=classify_tempo_relation(float(reference_bpm), float(candidate_bpm)),
    )


# ----- Beat position scoring ---------------------------------------------


@dataclass(frozen=True)
class PositionScore:
    n_reference: int
    n_candidate: int
    beat_count_ratio: float
    matched: int
    precision: float
    recall: float
    f_measure: float
    raw_p50_ms: float | None
    raw_p95_ms: float | None
    global_shift_ms: float | None
    shifted_p50_ms: float | None
    shifted_p95_ms: float | None


def _nearest_signed_errors_ms(
    reference: Sequence[float], candidate: Sequence[float]
) -> list[float]:
    """For each reference beat, the signed distance to the nearest candidate.

    Nearest-neighbour rather than index pairing, because a candidate that drops
    or adds beats would otherwise accumulate a meaningless index skew.
    """
    errors: list[float] = []
    for ref in reference:
        idx = bisect.bisect_left(candidate, ref)
        best: float | None = None
        for j in (idx - 1, idx):
            if 0 <= j < len(candidate):
                delta = candidate[j] - ref
                if best is None or abs(delta) < abs(best):
                    best = delta
        if best is not None:
            errors.append(best * 1000.0)
    return errors


def _one_to_one_matches(
    reference: Sequence[float], candidate: Sequence[float], tolerance_s: float
) -> int:
    """Greedy monotonic matching, each beat usable once.

    One-to-one matters: without it an analyzer emitting two beats per true beat
    would score full recall while being obviously wrong.
    """
    i = j = matched = 0
    while i < len(reference) and j < len(candidate):
        delta = candidate[j] - reference[i]
        if abs(delta) <= tolerance_s:
            matched += 1
            i += 1
            j += 1
        elif delta < 0:
            j += 1
        else:
            i += 1
    return matched


def score_positions(
    reference: Sequence[float],
    candidate: Sequence[float],
    tolerance_s: float = BEAT_TOLERANCE_S,
) -> PositionScore:
    """Score candidate beat times against rekordbox beat times over one window.

    Raises when the reference is empty: a track with no rekordbox grid has no
    ground truth, and scoring it would invent one.
    """
    ref = sorted(float(t) for t in reference)
    if not ref:
        raise ValueError("no reference beats: this track has no rekordbox grid to score against")
    cand = sorted(float(t) for t in candidate)

    if not cand:
        return PositionScore(
            n_reference=len(ref),
            n_candidate=0,
            beat_count_ratio=0.0,
            matched=0,
            precision=0.0,
            recall=0.0,
            f_measure=0.0,
            raw_p50_ms=None,
            raw_p95_ms=None,
            global_shift_ms=None,
            shifted_p50_ms=None,
            shifted_p95_ms=None,
        )

    signed = _nearest_signed_errors_ms(ref, cand)
    shift = _median(signed) or 0.0
    shifted = [e - shift for e in signed]

    matched = _one_to_one_matches(ref, cand, tolerance_s)
    precision = matched / len(cand)
    recall = matched / len(ref)
    f_measure = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0

    return PositionScore(
        n_reference=len(ref),
        n_candidate=len(cand),
        beat_count_ratio=len(cand) / len(ref),
        matched=matched,
        precision=precision,
        recall=recall,
        f_measure=f_measure,
        raw_p50_ms=percentile([abs(e) for e in signed], 50),
        raw_p95_ms=percentile([abs(e) for e in signed], 95),
        global_shift_ms=shift,
        shifted_p50_ms=percentile([abs(e) for e in shifted], 50),
        shifted_p95_ms=percentile([abs(e) for e in shifted], 95),
    )


# ----- Downbeat scoring ---------------------------------------------------


@dataclass(frozen=True)
class DownbeatScore:
    supported: bool
    n_reference: int
    n_candidate: int | None
    matched: int | None
    agreement: float | None


def score_downbeats(
    reference: Sequence[float],
    candidate: Sequence[float] | None,
    tolerance_s: float = BEAT_TOLERANCE_S,
) -> DownbeatScore:
    """Fraction of rekordbox bar-1 beats hit by a candidate downbeat.

    A candidate of None means the analyzer has no downbeat concept at all, and
    reports N/A. That is different from an analyzer that emits downbeats and
    gets them wrong, which scores 0.0.
    """
    ref = sorted(float(t) for t in reference)
    if candidate is None:
        return DownbeatScore(False, len(ref), None, None, None)
    cand = sorted(float(t) for t in candidate)
    if not ref:
        return DownbeatScore(True, 0, len(cand), 0, None)

    matched = 0
    for beat in ref:
        idx = bisect.bisect_left(cand, beat)
        for j in (idx - 1, idx):
            if 0 <= j < len(cand) and abs(cand[j] - beat) <= tolerance_s:
                matched += 1
                break
    return DownbeatScore(True, len(ref), len(cand), matched, matched / len(ref))
