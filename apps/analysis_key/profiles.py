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


#: The 24 candidates are one rotation per pitch class in each mode, indexed
#: ``2 * pitch_class + (1 if minor else 0)``. One index space, so the
#: correlation matrix, the Viterbi path and a segment's label cannot drift
#: apart the way two hand-kept orderings would.
N_KEYS = 24


def key_for_index(index: int) -> Key:
    if not 0 <= index < N_KEYS:
        raise ValueError(f"key index must be 0..{N_KEYS - 1}, got {index}")
    return Key(pitch_class=index // 2, is_minor=bool(index % 2))


def _unit(vector: np.ndarray) -> np.ndarray:
    return vector / (np.linalg.norm(vector) + 1e-9)


def _profile_matrix(major_profile: np.ndarray, minor_profile: np.ndarray) -> np.ndarray:
    """``(24, 12)``: row ``i`` is ``key_for_index(i)``'s profile, index-0-is-C.

    ``np.roll(profile, pitch_class)`` moves the profile's tonic (its position
    0) to the pitch class it is being scored as, which is the rotation
    `_estimate` performed one candidate at a time.
    """
    major_norm = _unit(major_profile)
    minor_norm = _unit(minor_profile)
    return np.stack(
        [
            np.roll(major_norm if index % 2 == 0 else minor_norm, index // 2)
            for index in range(N_KEYS)
        ]
    )


def correlation_matrix(
    chroma: np.ndarray,
    major_profile: np.ndarray | None = None,
    minor_profile: np.ndarray | None = None,
) -> np.ndarray:
    """``(24, T)``: one column per chroma column, the same scoring rule as a whole.

    Per COLUMN, not per mean: the key-change segmenter scores each bar against
    all 24 candidates, and it must be the same instrument the track-level
    estimate uses, not a second one that can disagree with it.
    """
    if chroma.ndim != 2 or chroma.shape[0] != 12:
        raise ValueError(f"chroma must have 12 pitch-class rows, got shape {chroma.shape}")
    matrix = _profile_matrix(
        _KRUMHANSL_MAJOR if major_profile is None else major_profile,
        _KRUMHANSL_MINOR if minor_profile is None else minor_profile,
    )
    return matrix @ _unit_columns(chroma)


def profile_correlations(
    chroma: np.ndarray,
    major_profile: np.ndarray | None = None,
    minor_profile: np.ndarray | None = None,
) -> np.ndarray:
    """``(24,)``: the correlations of the MEAN chroma, in `key_for_index` order.

    The long-term estimate's own score vector, exposed so segment confidence
    and the track's scalar key come from one computation.
    """
    if chroma.ndim != 2 or chroma.shape[0] != 12:
        raise ValueError(f"chroma must have 12 pitch-class rows, got shape {chroma.shape}")
    return correlation_matrix(
        chroma.mean(axis=1, keepdims=True), major_profile, minor_profile
    )[:, 0]


def _unit_columns(chroma: np.ndarray) -> np.ndarray:
    return chroma / (np.linalg.norm(chroma, axis=0, keepdims=True) + 1e-9)


#-----------------------------------------------------------------------------
def _estimate(
    chroma: np.ndarray, major_profile: np.ndarray, minor_profile: np.ndarray
) -> KeyEstimate:
    """Correlate a (12, T) chroma matrix against 24 rotated profile vectors.

    Chroma row 0 is pitch class C, matching librosa.feature.chroma_cqt and
    apps.analysis_key.canon.Key.pitch_class.
    """
    scores = profile_correlations(chroma, major_profile, minor_profile)
    ranked = sorted(
        ((key_for_index(index), float(score)) for index, score in enumerate(scores)),
        key=lambda pair: pair[1],
        reverse=True,
    )
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
