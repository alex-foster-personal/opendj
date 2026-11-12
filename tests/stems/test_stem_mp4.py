"""Stem reader null-test: prove or reject four-part reconstruction (issue #12234).

Uses small synthetic fixtures on disk. WAV containers exercise the same decode
path MP4/M4A stems use once expanded; no large binary blobs in the tree.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from tests.stems.null_test import (
    DEFAULT_RESIDUAL_THRESHOLD_DB,
    DEMUCS_PARTS,
    StemNullTestGeometryError,
    decode_stem_part,
    null_test_four_parts,
)

try:
    import soundfile as sf
except ImportError:
    sf = None  # type: ignore[assignment]

pytestmark = pytest.mark.requires_audio_stack

RATE = 11_025
FRAMES = 256


def _noise(seed: int, amp: float) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (amp * rng.uniform(-1.0, 1.0, size=(FRAMES, 2))).astype(np.float32)


def _write_demucs_bundle(root: Path, *, corrupt_vocals: bool = False) -> tuple[dict[str, Path], Path]:
    if sf is None:
        pytest.fail("requires_audio_stack should gate soundfile")
    root.mkdir(parents=True, exist_ok=True)
    parts = [_noise(i, 0.2) for i in range(4)]
    mix = np.sum(parts, axis=0)
    if corrupt_vocals:
        parts[0] = parts[0] + 0.4
    mix_path = root / "source.wav"
    sf.write(mix_path, mix, RATE)
    paths: dict[str, Path] = {}
    for name, audio in zip(DEMUCS_PARTS, parts, strict=True):
        part_path = root / f"{name}.wav"
        sf.write(part_path, audio, RATE)
        paths[name] = part_path
    return paths, mix_path


def test_stem_reader_proves_four_part_reconstruction(tmp_path: Path) -> None:
    """[if] reader decodes aligned parts [then] null test passes with residual dB, [else stop]."""
    parts, mix_path = _write_demucs_bundle(tmp_path / "good")
    result = null_test_four_parts(parts, mix_path)
    assert result.passed
    assert result.residual_db <= DEFAULT_RESIDUAL_THRESHOLD_DB
    assert result.frame_count == FRAMES


def test_stem_reader_rejects_mis_summed_parts(tmp_path: Path) -> None:
    """[if] one part does not belong in the mix [then] null test fails, [else stop]."""
    parts, mix_path = _write_demucs_bundle(tmp_path / "bad", corrupt_vocals=True)
    result = null_test_four_parts(parts, mix_path)
    assert not result.passed
    assert result.residual_db > DEFAULT_RESIDUAL_THRESHOLD_DB


def test_stem_reader_refuses_truncated_part(tmp_path: Path) -> None:
    """[if] one decoded part is shorter [then] refuse without truncating, [else stop]."""
    parts, mix_path = _write_demucs_bundle(tmp_path / "short")
    truncated = tmp_path / "short" / "vocals-trunc.wav"
    audio, rate = decode_stem_part(parts["vocals"])
    sf.write(truncated, audio[: FRAMES // 2], rate)
    broken = dict(parts)
    broken["vocals"] = truncated
    with pytest.raises(StemNullTestGeometryError, match="frame counts differ"):
        null_test_four_parts(broken, mix_path)


def test_each_part_decodes_to_expected_geometry(tmp_path: Path) -> None:
    """[if] synthetic bundle parts are read [then] frame count and rate match mix, [else stop]."""
    parts, mix_path = _write_demucs_bundle(tmp_path / "geometry")
    mix_audio, mix_rate = decode_stem_part(mix_path)
    for name, path in parts.items():
        audio, rate = decode_stem_part(path)
        assert rate == mix_rate
        assert audio.shape[0] == mix_audio.shape[0]
        assert audio.shape[1] == mix_audio.shape[1]
