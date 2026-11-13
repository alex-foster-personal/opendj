"""Self-decoded band peaks above 0 dBFS reach the detail lane unclipped (issue #9675).

Ported from PR #5967 (night branch af--night-1010--analysis-stems): main already
decodes from float with +6 dBFS headroom (NATIVE-12711), but the detail lane
still clipped every column at 1.0, so a loud master's overshooting low band
drew as a pinned full-height run. Every signal here is generated in the test.

Intent:
- [if] a low-passed square peaking at +3 dBFS pins any band byte at 255 [then] fail, [else stop].
- [if] the detail lane clips a column above 0 dBFS to 1.0 [then] fail, [else stop].
- [if] the engine decoder pins that square at 255 where it is available [then] fail, [else stop].
"""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
import pytest

from apps.analysis_waveform import decode
from apps.analysis_waveform.lane_payload import build_waveform_payload

pytestmark = pytest.mark.requirement("DECKUX-9675")

RATE = 44_100
SQUARE_HZ = 60.0
# A full-scale 60 Hz square peaks at about +1 dBFS after the 200 Hz 24 dB/oct
# lowpass (measured 144 of 255, Fri 13 Nov 2026), so this amplitude lands the
# filtered low band near +3 dBFS.
SQUARE_AMPLITUDE = 1.254
PLUS_3_DBFS = 10 ** (3 / 20)
FULL_SCALE_BYTE = decode._PEAK_SCALE / decode.PROFILE.peak_full_scale_linear


def _write_float_square_wav(path: Path, *, amplitude: float = SQUARE_AMPLITUDE, seconds: float = 2.0) -> Path:
    """Mono IEEE-float WAV square wave; float so the input itself may exceed 0 dBFS."""
    n = int(RATE * seconds)
    t = np.arange(n) / RATE
    samples = np.where((t * SQUARE_HZ) % 1.0 < 0.5, amplitude, -amplitude).astype("<f4")
    data = samples.tobytes()
    fmt = struct.pack("<HHIIHH", 3, 1, RATE, RATE * 4, 4, 32)
    body = (
        b"WAVE"
        + b"fmt "
        + struct.pack("<I", len(fmt))
        + fmt
        + b"fact"
        + struct.pack("<II", 4, n)
        + b"data"
        + struct.pack("<I", len(data))
        + data
    )
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    return path


def _assert_headroom_kept(peaks: np.ndarray) -> None:
    """No pinned 255 byte, and the detail lane carries the real +3 dBFS peak."""
    pinned = int((peaks == decode._PEAK_SCALE).sum())
    assert pinned == 0, f"{pinned} of {peaks.size} band bytes pinned at 255"
    steady = peaks[len(peaks) // 4 : -len(peaks) // 4, 0]
    low_db = 20 * np.log10(int(steady.max()) / FULL_SCALE_BYTE)
    assert 2.0 <= low_db <= 4.0, f"low band peaked at {low_db:+.2f} dBFS, expected about +3"
    detail = np.asarray(build_waveform_payload(peaks)["detail"]["low"])
    assert detail.max() > 1.2, f"detail low max {detail.max()} was clipped near 1.0"
    assert int((detail == 1.0).sum()) == 0, "detail columns pinned at the old 1.0 ceiling"


def test_detail_lane_keeps_a_column_above_zero_dbfs() -> None:
    """A +3 dBFS float column in the low band is served as about 1.41, not 1.0."""
    frames = struct.pack("<fff", PLUS_3_DBFS, 0.5, 0.0) * decode.SAMPLES_PER_COLUMN * 4
    peaks = decode._peak_columns(frames)
    assert int(peaks[:, 0].max()) < decode._PEAK_SCALE
    detail = build_waveform_payload(peaks)["detail"]
    assert detail["low"][0] == pytest.approx(PLUS_3_DBFS, abs=0.01)
    assert detail["mid"][0] == pytest.approx(0.5, abs=0.01)
    assert detail["high"][0] == 0.0


@pytest.mark.requires_ffmpeg
def test_ffmpeg_low_passed_square_at_plus_3_dbfs_does_not_pin(tmp_path: Path) -> None:
    """The ffmpeg decode of a +3 dBFS filtered square keeps its shape end to end."""
    source = _write_float_square_wav(tmp_path / "square-plus3.wav")
    _assert_headroom_kept(decode.decode_peaks_from("ffmpeg", source))


def test_engine_low_passed_square_at_plus_3_dbfs_does_not_pin(tmp_path: Path) -> None:
    """The engine decode of the same square keeps it too, when a build is present."""
    try:
        decode.resolve_engine()
    except decode.LocalDecodeUnavailable as exc:
        pytest.skip(f"no odj-audio build here: {exc.reason}")
    source = _write_float_square_wav(tmp_path / "square-plus3.wav")
    _assert_headroom_kept(decode.decode_peaks_from("engine", source))
