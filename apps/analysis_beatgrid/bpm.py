"""Ordinary BPM from beat times: a least-squares fit plus an explicit octave policy.

WHY LEAST SQUARES AND NOT THE MEDIAN INTER-BEAT INTERVAL. A frame-based model
emits beat times quantized to its frame rate (50 fps for Beat This!, so 20 ms).
The median of a quantized series is itself one of those quantized values, so
the derived tempo snaps to whatever `60 / (k * 0.02)` happens to be nearest and
lands a clean fraction of a BPM away from the truth. A least-squares fit of
beat TIME against beat INDEX averages the quantization out across the whole
window instead of inheriting it from one interval. Measured in round 0 of
specs/beat-mapping-bench.md on identical committed beat times: 47.0 percent of
fixed grids within 1.0 BPM by median interval, 82.5 percent by least squares.
The estimator, not the model, was 35 points of the answer.

WHY THE OCTAVE POLICY IS A SEPARATE, NAMED DECISION. "Found the pulse, chose
the wrong metrical level" is a different and far more recoverable failure than
"found the wrong pulse", so it is never allowed to happen silently. Every
estimate carries `octave_reason`, a short string naming which rule fired, and
`octave_ambiguous`, the spec's uncertainty flag for this lane.

THE REKORDBOX PRIOR IS A SCORING-ONLY INPUT AND THE CODE ENFORCES THAT.
specs/native-analysis-v1.md demotes rekordbox from source to reference: an own
producer that consulted rekordbox's stored BPM at runtime would not be an own
producer, it would be rekordbox laundered through a neural network, and the
whole benchmark would be measuring agreement with an input it was given. So
`estimate_bpm` REFUSES a prior unless `scoring=True` is passed explicitly. That
is a hard error rather than a silently ignored argument, because an ignored
argument is exactly the kind of defect that reads as working.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from apps.analysis_beatgrid.tempo_family import TempoFamily

# ----- Named constants ----------------------------------------------------

# The tempo band a DJ library's ordinary BPM is expected to land in. Its ratio
# is 2.57, wider than one octave, so TWO octaves of the same pulse can both sit
# inside it (80 and 160, say). That is precisely why ambiguity needs a flag
# rather than a tie-break nobody can see.
OCTAVE_RANGE_MIN_BPM = 70.0
OCTAVE_RANGE_MAX_BPM = 180.0

# Octave ladder searched, in multiples of the raw fitted tempo. Two steps each
# way covers 4x and 1/4x, which is past any metrical level a DJ track uses.
OCTAVE_MULTIPLES: tuple[float, ...] = (0.25, 0.5, 1.0, 2.0, 4.0)

# Below this many beats a straight-line fit is describing noise, not a tempo.
MIN_BEATS_FOR_FIT = 4

# Reasons, named once so a caller can branch on them without matching prose.
REASON_SINGLE_OCTAVE_IN_RANGE = "single_octave_in_range"
REASON_AMBIGUOUS_NEAREST_CENTER = "ambiguous_nearest_center"
REASON_AMBIGUOUS_MODEL_LEVEL = "ambiguous_model_level"
REASON_GENRE_FAMILY = "genre_family"
REASON_NO_OCTAVE_IN_RANGE = "no_octave_in_range"
REASON_PRIOR_SELECTED = "prior_selected"
REASON_PRIOR_SELECTED_OUT_OF_RANGE = "prior_selected_out_of_range"


# ----- The estimate -------------------------------------------------------


@dataclass(frozen=True)
class BpmEstimate:
    """One tempo reading and everything needed to audit how it was reached."""

    bpm: float
    raw_bpm: float
    octave_multiple: float
    octave_reason: str
    octave_ambiguous: bool
    n_beats: int
    # Root-mean-square residual of the straight-line fit, in seconds. This is
    # the honest confidence signal for a FIXED grid: a track whose beats do not
    # lie on a line has a large residual no matter how good the tempo looks.
    residual_rms_s: float
    confidence: float


# ----- The fit ------------------------------------------------------------


def least_squares_bpm(times: Sequence[float]) -> tuple[float, float] | None:
    """Fit `t_i = intercept + period * i` and return `(bpm, residual_rms_s)`.

    Returns None rather than a guess when there are too few beats or the fitted
    period is not positive. A caller that wanted a number gets an explicit
    absence, never a plausible-looking fabrication.
    """
    beats = [float(t) for t in times]
    n = len(beats)
    if n < MIN_BEATS_FOR_FIT:
        return None

    mean_i = (n - 1) / 2.0
    mean_t = sum(beats) / n
    # Closed form for the slope: sum((i - mean_i) * (t - mean_t)) / sum((i - mean_i)^2).
    numerator = sum((i - mean_i) * (t - mean_t) for i, t in enumerate(beats))
    denominator = sum((i - mean_i) ** 2 for i in range(n))
    if denominator <= 0:
        return None
    period_s = numerator / denominator
    if period_s <= 0:
        return None

    intercept = mean_t - period_s * mean_i
    residual_rms = math.sqrt(
        sum((t - (intercept + period_s * i)) ** 2 for i, t in enumerate(beats)) / n
    )
    return 60.0 / period_s, residual_rms


# ----- The octave policy --------------------------------------------------


def _in_range(bpm: float) -> bool:
    return OCTAVE_RANGE_MIN_BPM <= bpm <= OCTAVE_RANGE_MAX_BPM


def _log_distance(a: float, b: float) -> float:
    """Distance in log-tempo, so 64-vs-128 and 128-vs-256 are the same gap."""
    return abs(math.log(a) - math.log(b))


def choose_octave(
    raw_bpm: float,
    prior_bpm: float | None = None,
    *,
    scoring: bool = False,
    family: TempoFamily | None = None,
) -> tuple[float, float, str, bool]:
    """Pick a metrical level for `raw_bpm`; return `(bpm, multiple, reason, ambiguous)`.

    Rule order, and it never changes:
      1. When SCORING, the rekordbox prior picks the octave (attribution only).
      2. A genre tempo `family` (from the user's own genre tag, see
         `tempo_family.py`) picks the one octave inside its range, unflagged.
         No octave in its range means the family is ignored and rules 3-6
         decide, so a mistagged track cannot be forced off its pulse.
      3. Octaves inside [70, 180] are the only candidates while any exists.
      4. Exactly one such octave wins outright.
      5. More than one is genuinely ambiguous and flagged `octave_ambiguous`.
         The model's own metrical level wins when it is one of them; round 4
         measured the old tie-break (nearest the band's geometric center,
         112 BPM) halving every 163-175 BPM track the model tracked whole.
         Only when the model's level is outside the band does the center rule
         still pick.
      6. No octave in the band leaves the nearest one and the flag set.

    Raises when a prior is supplied without `scoring=True`. The prior is
    rekordbox data; letting it reach a runtime estimate would make the own
    producer a rekordbox reader with extra steps.
    """
    if prior_bpm is not None and not scoring:
        raise ValueError(
            "prior_bpm is a scoring-only input: the own producer must never consult "
            "rekordbox's stored BPM at runtime (specs/native-analysis-v1.md section 2). "
            "Pass scoring=True to use it for benchmark octave attribution."
        )
    if raw_bpm <= 0:
        raise ValueError(f"raw_bpm must be positive, got {raw_bpm}")

    candidates = [(m, raw_bpm * m) for m in OCTAVE_MULTIPLES]
    in_range = [(m, bpm) for m, bpm in candidates if _in_range(bpm)]

    if prior_bpm is not None:
        pool = in_range or candidates
        multiple, bpm = min(pool, key=lambda mb: _log_distance(mb[1], prior_bpm))
        reason = REASON_PRIOR_SELECTED if in_range else REASON_PRIOR_SELECTED_OUT_OF_RANGE
        return bpm, multiple, reason, not in_range

    if family is not None:
        in_family = [(m, bpm) for m, bpm in candidates if family.contains(bpm)]
        if in_family:
            multiple, bpm = min(
                in_family,
                key=lambda mb: _log_distance(mb[1], math.sqrt(family.min_bpm * family.max_bpm)),
            )
            return bpm, multiple, REASON_GENRE_FAMILY, False

    if len(in_range) == 1:
        multiple, bpm = in_range[0]
        return bpm, multiple, REASON_SINGLE_OCTAVE_IN_RANGE, False

    model_level = [(m, bpm) for m, bpm in in_range if m == 1.0]
    if len(in_range) > 1 and model_level:
        multiple, bpm = model_level[0]
        return bpm, multiple, REASON_AMBIGUOUS_MODEL_LEVEL, True

    if in_range:
        center = math.sqrt(OCTAVE_RANGE_MIN_BPM * OCTAVE_RANGE_MAX_BPM)
        multiple, bpm = min(in_range, key=lambda mb: _log_distance(mb[1], center))
        return bpm, multiple, REASON_AMBIGUOUS_NEAREST_CENTER, True

    center = math.sqrt(OCTAVE_RANGE_MIN_BPM * OCTAVE_RANGE_MAX_BPM)
    multiple, bpm = min(candidates, key=lambda mb: _log_distance(mb[1], center))
    return bpm, multiple, REASON_NO_OCTAVE_IN_RANGE, True


# ----- The public entry point ---------------------------------------------


def _confidence_from_residual(residual_rms_s: float, bpm: float, ambiguous: bool) -> float:
    """Residual as a fraction of one beat, inverted into a 0-1 rank score.

    NOT a probability, and named so nobody reads it as one: it is the fraction
    of a beat period the fit misses by, mapped so that a perfectly straight
    grid reads 1.0 and a grid whose beats scatter by a quarter of a beat reads
    0.0. An ambiguous octave halves it, because a confident reading at the
    wrong metrical level is still the wrong number.
    """
    period_s = 60.0 / bpm
    scatter = residual_rms_s / period_s if period_s > 0 else 1.0
    score = max(0.0, 1.0 - scatter / 0.25)
    return round(score * (0.5 if ambiguous else 1.0), 4)


def estimate_bpm(
    beat_times: Sequence[float],
    prior_bpm: float | None = None,
    *,
    scoring: bool = False,
    family: TempoFamily | None = None,
) -> BpmEstimate | None:
    """Ordinary BPM for one track's beat times, or None when there is no fit.

    `prior_bpm` is the rekordbox stored tempo and is accepted ONLY with
    `scoring=True`; `family` is the genre tempo family from the user's own tag;
    see `choose_octave`.
    """
    fit = least_squares_bpm(beat_times)
    if fit is None:
        return None
    raw_bpm, residual_rms_s = fit
    bpm, multiple, reason, ambiguous = choose_octave(
        raw_bpm, prior_bpm, scoring=scoring, family=family
    )
    return BpmEstimate(
        bpm=bpm,
        raw_bpm=raw_bpm,
        octave_multiple=multiple,
        octave_reason=reason,
        octave_ambiguous=ambiguous,
        n_beats=len(beat_times),
        residual_rms_s=residual_rms_s,
        confidence=_confidence_from_residual(residual_rms_s, bpm, ambiguous),
    )
