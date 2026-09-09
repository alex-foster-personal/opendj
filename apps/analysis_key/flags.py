"""The `no_tonal_center` low-confidence flag for the key lane.

WHY THIS EXISTS. Not every track has a clear tonal center to find: a long
ambient drone, a noise/FX intro, or a track built entirely from unpitched
percussion gives a Krumhansl-style correlation scorer nothing to lock onto.
Publishing SOME key label anyway (the scorer always returns its best of 24
candidates, never "none") would misrepresent a guess as a finding. This flag
is how that decline survives to the reader, following the same contract as
apps.analysis_beatgrid.flags.PulseFlag: a low-confidence result must go
inert with a reason, not silently pass as `status: ok`
(specs/native-analysis-v1.md section 3).

TWO INDEPENDENT SYMPTOMS, EITHER SUFFICIENT, mirroring PulseFlag's shape:

* `confidence` (the winning correlation) below threshold: even the best of
  24 candidates barely matched the chroma at all.
* `margin` (winning correlation minus the runner-up's) below threshold: the
  scorer found A key, but 23 semitone-and-mode rotations away sits an
  almost-equally-good alternative, which is what happens on chroma with no
  single dominant pitch-class center (e.g. a symmetric or noise-like chroma).

THRESHOLDS ARE UNCALIBRATED PLACEHOLDERS, NOT MEASURED. Unlike
ACTIVATION_PEAK_THRESHOLD in apps/analysis_beatgrid/flags.py (set from a
round-0 measurement over 137 tracks), nav1-key-r0 did not run a scored round
against real chroma before this file was written -- see specs/native-
analysis-v1.md's key-lane experiment log for why. Round 1 should replace
CONFIDENCE_THRESHOLD and MARGIN_THRESHOLD below with round-0-measured
thresholds once real confidence/margin distributions exist.

CONFIDENCE_THRESHOLD must clear the scorer's OWN reachable floor, not just
be "small". apps.analysis_key.profiles._estimate L2-normalizes both the
chroma and the (strictly positive) Krumhansl profiles before taking a
cosine score, and the best of 24 rotated candidates has a floor that is
provably NOT near zero: solving min over unit-norm nonnegative chroma of
max-over-24-rotations cosine score (a convex minimax program) gives ~0.489
for these exact profile constants -- an earlier CONFIDENCE_THRESHOLD=0.15
sat entirely below that floor, so the confidence branch below could
mathematically never fire for any nonzero chroma; only literal
all-silence (mean chroma exactly zero, scored as 0.0 by _estimate's
1e-9 norm guard) could ever trigger it, silently collapsing the "two
independent symptoms" design above to one. 0.5 is a principled margin
above the ~0.489 floor, not a measured one -- it exists only to keep this
check CAPABLE of firing before round-0 measurement replaces it outright.
"""
from __future__ import annotations

from dataclasses import dataclass

from apps.analysis_key.profiles import KeyEstimate

CONFIDENCE_THRESHOLD = 0.5
MARGIN_THRESHOLD = 0.02

REASON_LOW_CONFIDENCE = "low_confidence"
REASON_AMBIGUOUS_MARGIN = "ambiguous_margin"


@dataclass(frozen=True)
class TonalCenterFlag:
    no_tonal_center: bool
    reason: str | None
    confidence: float
    margin: float


def evaluate_tonal_center(
    estimate: KeyEstimate,
    *,
    confidence_threshold: float = CONFIDENCE_THRESHOLD,
    margin_threshold: float = MARGIN_THRESHOLD,
) -> TonalCenterFlag:
    """Decide whether `estimate` reflects a real tonal center worth publishing.

    Order is cause before consequence: an outright weak match is reported
    ahead of an ambiguous-but-passable one, since a confidence failure
    already explains why the margin might also be thin.
    """
    if estimate.confidence < confidence_threshold:
        return TonalCenterFlag(True, REASON_LOW_CONFIDENCE, estimate.confidence, estimate.margin)
    if estimate.margin < margin_threshold:
        return TonalCenterFlag(True, REASON_AMBIGUOUS_MARGIN, estimate.confidence, estimate.margin)
    return TonalCenterFlag(False, None, estimate.confidence, estimate.margin)
