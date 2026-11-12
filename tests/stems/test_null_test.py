"""Unit tests for ``tests.stems.null_test`` (issue #12234, PRD-STEMS-03)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from tests.stems.null_test import (
    DEFAULT_RESIDUAL_THRESHOLD_DB,
    DEMUCS_PARTS,
    StemNullTestGeometryError,
    null_test_decoded,
    null_test_four_parts,
    residual_db_against_mix,
)

try:
    import soundfile as sf
except ImportError:
    sf = None  # type: ignore[assignment]

pytestmark = pytest.mark.requires_audio_stack

RATE = 8_000
FRAMES = 512


def _require_sf():
    if sf is None:
        pytest.fail("requires_audio_stack should gate soundfile")
    return sf


def _part(seed: int, scale: float) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (scale * rng.uniform(-0.5, 0.5, size=(FRAMES, 2))).astype(np.float32)


def _aligned_bundle(scale: float = 0.25) -> tuple[list[np.ndarray], np.ndarray]:
    parts = [_part(i, scale) for i in range(4)]
    mix = np.sum(parts, axis=0)
    return parts, mix


def test_aligned_parts_within_threshold_pass() -> None:
    """[if] parts sum to mix within -60 dB [then] pass with numeric residual, [else stop]."""
    parts, mix = _aligned_bundle()
    result = null_test_decoded(parts, mix, RATE, threshold_db=DEFAULT_RESIDUAL_THRESHOLD_DB)
    assert result.passed
    assert result.residual_db <= DEFAULT_RESIDUAL_THRESHOLD_DB
    assert result.residual_db < -90.0
    assert result.sample_rate == RATE
    assert result.frame_count == FRAMES


def test_altered_part_fails_null_test() -> None:
    """[if] one part is shifted beyond threshold [then] fail, [else stop]."""
    parts, mix = _aligned_bundle()
    shifted = parts[0].copy()
    shifted[64:] = 0.0
    parts[0] = shifted
    result = null_test_decoded(parts, mix, RATE, threshold_db=DEFAULT_RESIDUAL_THRESHOLD_DB)
    assert not result.passed
    assert result.residual_db > DEFAULT_RESIDUAL_THRESHOLD_DB


def test_unequal_sample_counts_raise() -> None:
    """[if] unequal frame counts [then] error not pad, [else stop]."""
    parts, mix = _aligned_bundle()
    short = parts[0][: FRAMES // 2]
    with pytest.raises(StemNullTestGeometryError, match="frame counts differ"):
        null_test_decoded([short, *parts[1:]], mix, RATE)


def test_unequal_sample_rates_raise(tmp_path: Path) -> None:
    """[if] unequal sample rates [then] error not resample, [else stop]."""
    lib = _require_sf()
    parts, mix = _aligned_bundle()
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    mix_path = bundle / "mix.wav"
    lib.write(mix_path, mix, RATE)
    part_paths = {}
    for idx, name in enumerate(DEMUCS_PARTS):
        path = bundle / f"{name}.wav"
        rate = RATE if idx < 3 else RATE // 2
        lib.write(path, parts[idx], rate)
        part_paths[name] = path
    with pytest.raises(StemNullTestGeometryError, match="sample rates differ"):
        null_test_four_parts(part_paths, mix_path)


def test_on_disk_aligned_wav_fixture_passes(tmp_path: Path) -> None:
    """[if] on-disk aligned WAV parts sum to mix [then] decode path passes, [else stop]."""
    lib = _require_sf()
    parts, mix = _aligned_bundle()
    root = tmp_path / "wav-bundle"
    root.mkdir()
    mix_path = root / "mix.wav"
    lib.write(mix_path, mix, RATE)
    part_paths = {}
    for name, audio in zip(DEMUCS_PARTS, parts, strict=True):
        path = root / f"{name}.wav"
        lib.write(path, audio, RATE)
        part_paths[name] = path
    result = null_test_four_parts(part_paths, mix_path)
    assert result.passed
    assert isinstance(result.residual_db, float)


def test_residual_db_reports_relative_level() -> None:
    """[if] residual is half the mix RMS [then] reports about -6 dB, [else stop]."""
    mix = np.ones((128, 2), dtype=np.float32)
    residual = 0.5 * mix
    db = residual_db_against_mix(residual, mix)
    assert db == pytest.approx(-6.02, abs=0.05)


def test_stereo_opposition_does_not_falsely_pass_via_downmix() -> None:
    """[if] L/R residual cancels when averaged [then] all-channel RMS still fails, [else stop]."""
    parts, mix = _aligned_bundle()
    # Equal and opposite L/R corruption: mono downmix of the residual would read silence.
    parts[0][:, 0] += 0.5
    parts[0][:, 1] -= 0.5
    result = null_test_decoded(parts, mix, RATE)
    assert not result.passed
    assert result.residual_db > DEFAULT_RESIDUAL_THRESHOLD_DB


def test_unequal_channel_counts_raise() -> None:
    """[if] one part is mono and others stereo [then] error not broadcast, [else stop]."""
    parts, mix = _aligned_bundle()
    parts[0] = parts[0].mean(axis=1, keepdims=False)
    with pytest.raises(StemNullTestGeometryError, match="channel counts differ"):
        null_test_decoded(parts, mix, RATE)
