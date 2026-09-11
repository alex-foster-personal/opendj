"""Versioned, dependency-free scoring for lyric word-onset alignment.

Predicted onsets are index-aligned to reference word onsets. A ``None``
prediction means the aligner did not place that reference word. Accuracy
metrics use the ``words_scored`` denominator, while recall keeps unplaced
words visible as required by the ratified lyrics-over-waveforms spike spec.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from math import isfinite
from statistics import median

SCORER_VERSION = "1.0.0"
PCO_300MS_TOLERANCE_S = 0.3
CATASTROPHE_TOLERANCE_S = 2.0
_FLOAT_SLACK_S = 1e-9

SHIP_MEDAE_S_MAX = 0.15
SHIP_PCO_300MS_MIN = 0.90
SHIP_CATASTROPHE_RATE_MAX = 0.03


@dataclass(frozen=True)
class LyricAlignScore:
    """The ratified scorer's three quality metrics and named denominators."""

    medae_s: float
    pco_300ms: float
    catastrophe_rate: float
    words_scored: int
    words_reference: int
    words_unplaced: int
    recall: float


def lyric_align_score(
    reference_onsets_s: Sequence[float], predicted_onsets_s: Sequence[float | None]
) -> LyricAlignScore:
    """Score index-aligned predicted word onsets against reference track seconds.

    Raises:
        ValueError: The input cannot represent a complete reference-word pairing.
    """
    if not reference_onsets_s:
        raise ValueError("no reference word onsets: scoring requires ground truth")
    if len(reference_onsets_s) != len(predicted_onsets_s):
        raise ValueError("reference and predicted onsets must have the same number of word rows")

    absolute_errors_s: list[float] = []
    for index, (reference_s, predicted_s) in enumerate(
        zip(reference_onsets_s, predicted_onsets_s, strict=True)
    ):
        _validate_track_second(reference_s, "reference", index)
        if predicted_s is None:
            continue
        _validate_track_second(predicted_s, "predicted", index)
        absolute_errors_s.append(abs(float(predicted_s) - float(reference_s)))

    words_reference = len(reference_onsets_s)
    words_scored = len(absolute_errors_s)
    if not words_scored:
        raise ValueError("no predicted word onsets: accuracy metrics have no denominator")

    return LyricAlignScore(
        medae_s=float(median(absolute_errors_s)),
        pco_300ms=sum(
            error <= PCO_300MS_TOLERANCE_S + _FLOAT_SLACK_S for error in absolute_errors_s
        )
        / words_scored,
        catastrophe_rate=sum(error > CATASTROPHE_TOLERANCE_S for error in absolute_errors_s)
        / words_scored,
        words_scored=words_scored,
        words_reference=words_reference,
        words_unplaced=words_reference - words_scored,
        recall=words_scored / words_reference,
    )


def _validate_track_second(value: float, role: str, index: int) -> None:
    """Reject non-track-time values before they can produce a plausible score."""
    if not isfinite(value) or value < 0:
        raise ValueError(f"{role} onset at word row {index} must be a finite track second >= 0")
