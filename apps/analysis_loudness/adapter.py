"""Thin native-analysis adapter for the shipped ffmpeg R128 scanner.

Mini-PRD
--------
R1 done  Return integrated LUFS, true peak dBTP, loudness range LU and whole-
         file RMS dB for one decodable audio file.
R2 done  Preserve the scanner's fail-fast errors. Missing ffmpeg, decode
         failures and an unparseable RMS result never become a default value.

Acceptance
----------
[if] a -23 LUFS sine is analyzed [then] LUFS is within 0.1 LU [else failed]
[if] a near-Nyquist sine overshoots [then] true peak is above 0 dBTP [else failed]
[if] ffmpeg is unavailable [then] LoudnessError is raised [else failed]
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from apps.loudness.scan import SCAN_TIMEOUT_S, LoudnessError, require_ffmpeg, scan_file

_RMS_PATTERN = re.compile(
    r"^.*RMS level dB:\s*(-?\d+(?:\.\d+)?|-inf)\s*$", re.M
)


@dataclass(frozen=True)
class LoudnessAnalysis:
    """Measured native loudness values for one audio file."""

    integrated_lufs: float
    true_peak_dbtp: float
    loudness_range_lu: float
    rms_db: float


def _scan_whole_file_rms(path: Path, binary: str) -> float:
    """Measure overall RMS with ffmpeg astats, failing on every bad state."""
    completed = subprocess.run(
        [
            binary,
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-map",
            "a:0",
            "-af",
            "astats=metadata=0:reset=0",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=SCAN_TIMEOUT_S,
        check=False,
    )
    if completed.returncode != 0:
        raise LoudnessError(
            f"ffmpeg exited {completed.returncode} measuring RMS for {path}:\n"
            f"{completed.stderr[-2000:]}"
        )
    matches = _RMS_PATTERN.findall(completed.stderr)
    if not matches:
        raise LoudnessError(
            f"ffmpeg astats output for {path} has no overall RMS level dB line. "
            f"ffmpeg stderr tail:\n{completed.stderr[-2000:]}"
        )
    raw = matches[-1]
    return float("-inf") if raw == "-inf" else float(raw)


def analyze_file(path: Path) -> LoudnessAnalysis:
    """Measure native loudness values for ``path`` with the shipped scanner.

    Resolves ffmpeg once and shares it across both passes: a PATH change
    between them would otherwise let the R128 and RMS measurements come from
    two different ffmpeg binaries.
    """
    binary = require_ffmpeg()
    scan = scan_file(path, binary=binary)
    return LoudnessAnalysis(
        integrated_lufs=scan.integrated_lufs,
        true_peak_dbtp=scan.true_peak_dbtp,
        loudness_range_lu=scan.loudness_range_lu,
        rms_db=_scan_whole_file_rms(path, binary),
    )
