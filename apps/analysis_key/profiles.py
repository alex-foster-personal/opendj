"""Krumhansl-Schmuckler style key-profile scoring against a chroma vector.

`estimate_key_krumhansl` correlates a mean chroma vector against 12 rotations
each of a major and a minor reference profile, and reports the rotation/mode
with the highest correlation. This module takes a (12, T) chroma matrix,
never raw audio, so it is testable against synthetic chroma without an audio
decoder (apps/analysis_key has no librosa dependency of its own; a caller
supplies chroma however it likes, e.g. via apps.analysis.backends.librosa's
librosa.feature.chroma_cqt).

PROFILE PROVENANCE.

krumhansl: Krumhansl & Kessler (1982) correlation profiles, ported verbatim
from apps/analysis/backends/librosa.py's `_KS_MAJOR`/`_KS_MINOR` (this module
is now that logic's single source; librosa.py should migrate to call it
rather than keep a second copy -- tracked as follow-up, not done here to keep
this PR's diff to the new lane).

An EDM-tuned candidate (edma/bgate, Faraldo, Jorda & Herrera 2017, "A
Multi-Profile Method for Key Estimation in EDM," AES Conference on Semantic
Audio) was attempted in this round and removed: this repo's own
specs/native-analysis-v1.md section 5.1 states the provenance rule for those
constants explicitly -- transcribed from the Faraldo papers themselves, never
from Essentia's AGPL-licensed key.cpp or the unlicensed edmkey repository.
The values previously here were sourced from Essentia's source, which
violates that rule regardless of intent (citation-only, no code linked); they
are removed rather than kept under a wrong citation. Re-adding an EDM profile
needs either the primary paper's own published table or an independent fit
on our own library's AGREE bucket (same section); both are follow-up work,
tracked in issue #1601.

Every profile array is index-0-is-tonic (the value at array position 0 is
the weight of the tonic's own pitch class; position 7 is the fifth, etc.),
matching the convention `_estimate` rotates against a fixed pitch-class-0-is-C
chroma vector via `np.roll`.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from apps.analysis_key.canon import Key

# Krumhansl & Kessler (1982), ported from apps/analysis/backends/librosa.py.
_KRUMHANSL_MAJOR = np.array(
    [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88],
    dtype=np.float64,
)
_KRUMHANSL_MINOR = np.array(
    [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17],
    dtype=np.float64,
)


@dataclass(frozen=True)
class KeyEstimate:
    """A candidate's output: a key guess, its raw correlation score, and the
    margin over the second-best of the 24 rotation/mode candidates. A high
    confidence with a near-zero margin means the profile is nearly as happy
    with a different key -- the ambiguity `flags.no_tonal_center` checks for.
    """

    key: Key
    confidence: float
    margin: float


#-----------------------------------------------------------------------------
def _estimate(
    chroma: np.ndarray, major_profile: np.ndarray, minor_profile: np.ndarray
) -> KeyEstimate:
    """Correlate a (12, T) chroma matrix against 24 rotated profile vectors.

    Chroma row 0 is pitch class C, matching librosa.feature.chroma_cqt and
    apps.analysis_key.canon.Key.pitch_class.
    """
    if chroma.shape[0] != 12:
        raise ValueError(f"chroma must have 12 pitch-class rows, got shape {chroma.shape}")
    mean = chroma.mean(axis=1)
    mean = mean / (np.linalg.norm(mean) + 1e-9)
    major_norm = major_profile / (np.linalg.norm(major_profile) + 1e-9)
    minor_norm = minor_profile / (np.linalg.norm(minor_profile) + 1e-9)

    correlations: list[tuple[Key, float]] = []
    for pitch_class in range(12):
        major_rolled = np.roll(major_norm, pitch_class)
        minor_rolled = np.roll(minor_norm, pitch_class)
        correlations.append((Key(pitch_class, False), float(np.dot(mean, major_rolled))))
        correlations.append((Key(pitch_class, True), float(np.dot(mean, minor_rolled))))

    ranked = sorted(correlations, key=lambda pair: pair[1], reverse=True)
    best_key, best_corr = ranked[0]
    _, second_corr = ranked[1]
    return KeyEstimate(
        key=best_key,
        confidence=max(0.0, best_corr),
        margin=max(0.0, best_corr - second_corr),
    )


#-----------------------------------------------------------------------------
def estimate_key_krumhansl(chroma: np.ndarray) -> KeyEstimate:
    return _estimate(chroma, _KRUMHANSL_MAJOR, _KRUMHANSL_MINOR)
