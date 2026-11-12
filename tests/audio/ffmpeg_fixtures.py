"""ffmpeg fixture audio for tests.audio (resolved binary, same as loudness scan)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from apps.loudness.scan import require_ffmpeg


def resolved_ffmpeg() -> str:
    """The same ffmpeg binary :func:`apps.loudness.scan.scan_file` uses."""
    return require_ffmpeg()


def sine_wav(path: Path, amplitude: float, seconds: int = 5) -> Path:
    subprocess.run(
        [
            resolved_ffmpeg(),
            "-hide_banner",
            "-nostats",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"aevalsrc={amplitude}*sin(2*PI*1000*t):d={seconds}:s=48000",
            "-c:a",
            "pcm_s16le",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def attenuated_copy(source: Path, dest: Path, gain_db: float) -> Path:
    subprocess.run(
        [
            resolved_ffmpeg(),
            "-hide_banner",
            "-nostats",
            "-y",
            "-i",
            str(source),
            "-af",
            f"volume={gain_db}dB",
            str(dest),
        ],
        check=True,
        capture_output=True,
    )
    return dest
