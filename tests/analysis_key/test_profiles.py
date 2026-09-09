"""Tests for apps.analysis_key.profiles against synthetic chroma.

No audio decoding involved: each chroma matrix is hand-built to look like a
librosa.feature.chroma_cqt output (12 pitch-class rows, C=0) dominated by a
triad, so every candidate profile (which all peak sharply at the tonic and
moderately at the third/fifth) should agree on the same key.
"""
from __future__ import annotations

import numpy as np
import pytest

from apps.analysis_key.canon import Key
from apps.analysis_key.profiles import estimate_key_krumhansl

_CANDIDATES = [estimate_key_krumhansl]


def _triad_chroma(root_pc: int, third_offset: int, frames: int = 8) -> np.ndarray:
    """A (12, frames) chroma matrix peaked at root/third/fifth of `root_pc`."""
    column = np.full(12, 0.05)
    column[0] = 1.0
    column[third_offset] = 0.5
    column[7] = 0.7
    column = np.roll(column, root_pc)
    return np.tile(column[:, None], (1, frames))


@pytest.mark.parametrize("estimate_key", _CANDIDATES)
def test_c_major_triad_chroma_scores_as_c_major(estimate_key) -> None:
    chroma = _triad_chroma(root_pc=0, third_offset=4)
    result = estimate_key(chroma)
    assert result.key == Key(pitch_class=0, is_minor=False)
    assert result.confidence > 0.0


@pytest.mark.parametrize("estimate_key", _CANDIDATES)
def test_a_minor_triad_chroma_scores_as_a_minor(estimate_key) -> None:
    chroma = _triad_chroma(root_pc=9, third_offset=3)
    result = estimate_key(chroma)
    assert result.key == Key(pitch_class=9, is_minor=True)
    assert result.confidence > 0.0


@pytest.mark.parametrize("estimate_key", _CANDIDATES)
def test_f_sharp_major_triad_chroma_scores_as_f_sharp_major(estimate_key) -> None:
    chroma = _triad_chroma(root_pc=6, third_offset=4)
    result = estimate_key(chroma)
    assert result.key == Key(pitch_class=6, is_minor=False)


def test_rejects_chroma_without_twelve_rows() -> None:
    bad_chroma = np.zeros((10, 8))
    with pytest.raises(ValueError):
        estimate_key_krumhansl(bad_chroma)


def test_silence_still_returns_a_key_with_zero_confidence() -> None:
    silence = np.zeros((12, 8))
    result = estimate_key_krumhansl(silence)
    assert isinstance(result.key, Key)
    assert result.confidence == 0.0
