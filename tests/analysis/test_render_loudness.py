"""Tests for tests.audio.render_loudness (offline render loudness hygiene)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from apps.loudness.scan import LoudnessError, require_ffmpeg
from tests.audio.ffmpeg_fixtures import attenuated_copy, sine_wav
from tests.audio.render_loudness import (
    DEFAULT_TOLERANCE_LU,
    TOLERANCE_BOUNDARY_EPS_LU,
    LoudnessPair,
    RenderLoudnessMismatchError,
    assert_render_loudness_match,
    compare_render_loudness,
    measure_integrated_lufs,
)


def _ffmpeg_available() -> bool:
    try:
        require_ffmpeg()
        return True
    except LoudnessError:
        return False


pytestmark = pytest.mark.skipif(
    not _ffmpeg_available(),
    reason="integrated loudness uses resolved ffmpeg (MDT_FFMPEG or PATH)",
)


def test_within_tolerance_passes(tmp_path: Path) -> None:
    """[if] source and render differ by 0.5 LU or less [then] compare passes, [else stop]."""
    source = sine_wav(tmp_path / "source.wav", 0.5)
    render = sine_wav(tmp_path / "render.wav", 0.5)
    pair = assert_render_loudness_match(source, render)
    assert pair.delta_lu <= DEFAULT_TOLERANCE_LU


def test_within_tolerance_allows_small_gain_delta(tmp_path: Path) -> None:
    """[if] render is 0.4 LU quieter than source [then] assert passes, [else stop]."""
    source = sine_wav(tmp_path / "source.wav", 0.5)
    render = attenuated_copy(source, tmp_path / "render.wav", gain_db=-0.4)
    pair = assert_render_loudness_match(source, render, tolerance_lu=0.5)
    assert pair.delta_lu <= 0.5


def test_exact_tolerance_boundary_allows_float_rounding() -> None:
    """[if] delta is 0.5 LU within float noise [then] assert passes, [else stop]."""
    pair = LoudnessPair(source_lufs=-7.8, render_lufs=-8.3)
    assert pair.delta_lu > DEFAULT_TOLERANCE_LU
    assert pair.delta_lu <= DEFAULT_TOLERANCE_LU + TOLERANCE_BOUNDARY_EPS_LU
    with patch(
        "tests.audio.render_loudness.compare_render_loudness",
        return_value=pair,
    ):
        assert_render_loudness_match(Path("s.wav"), Path("r.wav"), tolerance_lu=0.5)


def test_exceeding_tolerance_fails_with_both_lufs(tmp_path: Path) -> None:
    """[if] source and render differ by more than 0.5 LU [then] assert fails naming both, [else stop]."""
    source = sine_wav(tmp_path / "source.wav", 0.5)
    render = sine_wav(tmp_path / "render.wav", 0.05)
    with pytest.raises(RenderLoudnessMismatchError) as caught:
        assert_render_loudness_match(source, render)
    exc = caught.value
    assert f"{exc.source_lufs:.2f}" in str(exc)
    assert f"{exc.render_lufs:.2f}" in str(exc)
    measured = compare_render_loudness(source, render)
    assert exc.source_lufs == pytest.approx(measured.source_lufs, abs=0.01)
    assert exc.render_lufs == pytest.approx(measured.render_lufs, abs=0.01)
    assert measured.delta_lu > DEFAULT_TOLERANCE_LU + TOLERANCE_BOUNDARY_EPS_LU


def test_unreadable_source_raises_without_fabricated_lufs(tmp_path: Path) -> None:
    """[if] source is unreadable [then] LoudnessError and no LUFS return, [else stop]."""
    junk = tmp_path / "junk.wav"
    junk.write_bytes(b"not audio")
    render = sine_wav(tmp_path / "render.wav", 0.5)
    with pytest.raises(LoudnessError):
        measure_integrated_lufs(junk)
    with pytest.raises(LoudnessError):
        assert_render_loudness_match(junk, render)


def test_unreadable_render_raises_without_fabricated_lufs(tmp_path: Path) -> None:
    """[if] render is unreadable [then] LoudnessError and no LUFS return, [else stop]."""
    source = sine_wav(tmp_path / "source.wav", 0.5)
    missing = tmp_path / "missing.wav"
    with pytest.raises(LoudnessError, match="not a file"):
        assert_render_loudness_match(source, missing)
