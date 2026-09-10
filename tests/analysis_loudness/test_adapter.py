"""Real ffmpeg regression coverage for the native loudness producer.

[if] native loudness measurement regresses [then] fail, [else stop].
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from apps.analysis_loudness import analyze_file

pytestmark = pytest.mark.requirement("NATIVE-07")


def _sine(path: Path, amplitude: float, frequency_hz: int = 1000) -> Path:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg is required to run NATIVE-07 acceptance tests")
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostats", "-y",
            "-f", "lavfi",
            "-i", (
                f"aevalsrc={amplitude}*sin(2*PI*{frequency_hz}*t):"
                "d=5:s=44100"
            ),
            "-c:a", "pcm_s16le", str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


# if a synthetic -23 LUFS sine is outside 0.1 LU then the producer is not
# carrying the R128 measurement through unchanged
def test_adapter_reports_minus_23_lufs_sine(tmp_path: Path) -> None:
    """[if] a -23 LUFS sine changes measurement [then] fail, [else stop]."""
    result = analyze_file(_sine(tmp_path / "target.wav", 0.1))
    assert result.integrated_lufs == pytest.approx(-23.0, abs=0.1)
    assert result.rms_db == pytest.approx(-23.01, abs=0.2)


# if an inter-sample peak above 0 dBTP is not reported then the ebur128 peak
# setting is sample peak rather than true peak
def test_adapter_reports_inter_sample_true_peak(tmp_path: Path) -> None:
    """[if] a true-peak overshoot is lost [then] fail, [else stop]."""
    result = analyze_file(_sine(tmp_path / "overshoot.wav", 0.999, 19000))
    assert result.true_peak_dbtp > 0.0


# if the loudness gate compares LUFS against RMS dB or against a rekordbox
# column then broken
def test_lufs_and_rms_are_distinct_measurements(tmp_path: Path) -> None:
    """[if] LUFS and RMS collapse to one value [then] fail, [else stop]."""
    result = analyze_file(_sine(tmp_path / "distinct.wav", 0.5, 19000))
    assert result.integrated_lufs > result.rms_db + 2.0


# if ffmpeg is missing then LoudnessError is raised, never a default result
def test_missing_ffmpeg_raises_loudness_error(tmp_path: Path) -> None:
    """[if] ffmpeg is absent [then] fail loudly, [else stop]."""
    input_path = _sine(tmp_path / "input.wav", 0.5)
    code = (
        "from pathlib import Path\n"
        "from apps.analysis_loudness import analyze_file\n"
        f"analyze_file(Path({str(input_path)!r}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": ""},
        check=False,
    )
    assert result.returncode != 0
    assert "not on PATH" in result.stderr
