"""Shared ffmpeg-generated WAV fixtures for analysis_loudness tests."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def require_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg is required to run NATIVE-07 acceptance tests")


def write_sine_wav(
    path: Path,
    *,
    amplitude: float = 0.1,
    frequency_hz: int = 1000,
    duration_s: float = 5.0,
    sample_rate: int = 44100,
) -> Path:
    """Write a mono PCM sine WAV at ``path`` and return ``path``."""
    require_ffmpeg()
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-y",
            "-f",
            "lavfi",
            "-i",
            (
                f"aevalsrc={amplitude}*sin(2*PI*{frequency_hz}*t):"
                f"d={duration_s}:s={sample_rate}"
            ),
            "-c:a",
            "pcm_s16le",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def write_silence_wav(
    path: Path,
    *,
    duration_s: float = 5.0,
    sample_rate: int = 44100,
    channels: str = "stereo",
) -> Path:
    """Write a silent PCM WAV at ``path`` and return ``path``."""
    require_ffmpeg()
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"anullsrc=r={sample_rate}:cl={channels}",
            "-t",
            str(duration_s),
            "-c:a",
            "pcm_s16le",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path
