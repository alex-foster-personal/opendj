"""Regression tests for apps.loudness.

Fixtures are generated with ffmpeg rather than mocked: the whole point of this
module is that it agrees with a real ffmpeg, so a mocked ffmpeg would test
nothing. Amplitudes are set with aevalsrc, NOT the lavfi `sine` source, which
is not full scale (it measures -18.1 dBFS) and silently shifts every expected
number by 18 dB.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from apps.loudness.scan import LoudnessError, LoudnessScan, _parse_summary, scan_file

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="apps.loudness measures with real ffmpeg"
)


def _sine(path: Path, amplitude: float, seconds: int = 5) -> Path:
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostats", "-y",
            "-f", "lavfi",
            "-i", f"aevalsrc={amplitude}*sin(2*PI*1000*t):d={seconds}:s=48000",
            "-c:a", "pcm_s16le", str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


# if a -6 dBFS sine does not measure -6 dBTP then the true-peak parse is wrong
def test_true_peak_matches_generated_amplitude(tmp_path: Path) -> None:
    scan = scan_file(_sine(tmp_path / "half.wav", 0.5))
    assert scan.true_peak_dbtp == pytest.approx(-6.0, abs=0.5)


# if integrated loudness is not far below 0 then LUFS is being read as dBFS
def test_integrated_loudness_is_lufs(tmp_path: Path) -> None:
    scan = scan_file(_sine(tmp_path / "half.wav", 0.5))
    assert -20.0 < scan.integrated_lufs < -3.0


# if a quieter file does not report lower loudness then the field is static
def test_quieter_file_reports_lower_loudness(tmp_path: Path) -> None:
    loud = scan_file(_sine(tmp_path / "loud.wav", 0.5))
    quiet = scan_file(_sine(tmp_path / "quiet.wav", 0.05))
    assert quiet.integrated_lufs < loud.integrated_lufs - 15.0


# if a missing file reaches ffmpeg then the guard clause is gone
def test_missing_file_raises_before_ffmpeg(tmp_path: Path) -> None:
    with pytest.raises(LoudnessError, match="not a file"):
        scan_file(tmp_path / "nope.wav")


# if a non-audio file scans clean then ffmpeg failures are being swallowed
def test_non_audio_file_raises(tmp_path: Path) -> None:
    junk = tmp_path / "junk.wav"
    junk.write_bytes(b"not audio at all")
    with pytest.raises(LoudnessError):
        scan_file(junk)


# if a summary missing a field parses then a partial result can escape
def test_partial_summary_names_the_missing_field() -> None:
    partial = "  Integrated loudness:\n    I:         -14.0 LUFS\n"
    with pytest.raises(LoudnessError, match="loudness_range_lu"):
        _parse_summary(partial, Path("x.wav"))


# if the running per-frame lines win over the summary then the value is a
# mid-track sample, not the integrated figure
def test_summary_wins_over_running_lines() -> None:
    stderr = (
        "[ebur128] t: 1.0 I: -30.0 LUFS LRA: 1.0 LU\n"
        "  Integrated loudness:\n    I:         -14.0 LUFS\n"
        "  Loudness range:\n    LRA:         6.0 LU\n"
        "  True peak:\n    Peak:       -1.5 dBFS\n"
    )
    fields = _parse_summary(stderr, Path("x.wav"))
    assert fields["integrated_lufs"] == -14.0
    assert fields["true_peak_dbtp"] == -1.5


# if gain is not clamped then applying it drives true peak over the ceiling
def test_gain_clamped_by_peak_ceiling() -> None:
    scan = LoudnessScan(Path("x"), integrated_lufs=-30.0, loudness_range_lu=5.0,
                        true_peak_dbtp=-2.0)
    gain, clamped = scan.gain_db(target_lufs=-14.0, ceiling_dbtp=-1.0)
    assert clamped is True
    assert gain == pytest.approx(1.0)  # only 1 dB of peak headroom exists


# if a track with headroom reports clamped then the clamp is unconditional
def test_gain_unclamped_when_headroom_allows() -> None:
    scan = LoudnessScan(Path("x"), integrated_lufs=-20.0, loudness_range_lu=5.0,
                        true_peak_dbtp=-12.0)
    gain, clamped = scan.gain_db(target_lufs=-14.0, ceiling_dbtp=-1.0)
    assert clamped is False
    assert gain == pytest.approx(6.0)


# --- ffmpeg resolution (NATIVE-10) -----------------------------------------
# An installed app is launched without Homebrew's PATH, so the packaged
# engine names ffmpeg through MDT_FFMPEG. The loudness lane used to resolve
# ffmpeg with its own bare PATH lookup, the one resolver in the analysis
# lanes that ignored MDT_FFMPEG: the offline acceptance run found every
# own_loudness.backfill drain stopping with "'ffmpeg' is not on PATH" while
# waveform, key and beatgrid (on apps.shared.ffmpeg) decoded the same files.


@pytest.mark.requirement("NATIVE-10")
def test_require_ffmpeg_honors_mdt_ffmpeg_with_no_ffmpeg_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] only MDT_FFMPEG names ffmpeg [then] loudness still finds it, [else stop]."""
    from apps.loudness.scan import require_ffmpeg

    real = shutil.which("ffmpeg")
    if real is None:
        pytest.skip("needs a real ffmpeg to name through MDT_FFMPEG")
    monkeypatch.setenv("PATH", str(tmp_path))  # an empty directory: no ffmpeg
    monkeypatch.setenv("MDT_FFMPEG", real)
    assert require_ffmpeg() == real


@pytest.mark.requirement("NATIVE-10")
def test_require_ffmpeg_still_fails_loudly_with_neither(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] neither MDT_FFMPEG nor PATH has ffmpeg [then] a named error, [else stop]."""
    from apps.loudness.scan import require_ffmpeg

    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("MDT_FFMPEG", raising=False)
    with pytest.raises(LoudnessError, match="MDT_FFMPEG"):
        require_ffmpeg()
