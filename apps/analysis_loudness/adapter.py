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

import math
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from apps.analysis.lanes import LaneResult
from apps.loudness.scan import SCAN_TIMEOUT_S, LoudnessError, require_ffmpeg, scan_file

_RMS_PATTERN = re.compile(r"^.*RMS level dB:\s*(-?\d+(?:\.\d+)?|-inf)\s*$", re.M)
_STDERR_TAIL_CHARS = 2000


def _stderr_tail(stderr: str) -> str:
    return stderr[-_STDERR_TAIL_CHARS:]


@dataclass(frozen=True)
class LoudnessAnalysis:
    """Measured native loudness values for one audio file."""

    integrated_lufs: float
    true_peak_dbtp: float
    loudness_range_lu: float
    rms_db: float

    def to_lane_result(self) -> LaneResult:
        """Map this measurement onto the ``loudness`` lane payload contract."""
        payload = asdict(self)
        if not all(math.isfinite(value) for value in payload.values()):
            return LaneResult(status="failed", reason="non_finite_measurement")
        return LaneResult(status="ok", payload=payload)


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
            f"ffmpeg exited {completed.returncode} measuring RMS for {path}:\n{_stderr_tail(completed.stderr)}"
        )
    matches = _RMS_PATTERN.findall(completed.stderr)
    if not matches:
        raise LoudnessError(
            f"ffmpeg astats output for {path} has no overall RMS level dB line. "
            f"ffmpeg stderr tail:\n{_stderr_tail(completed.stderr)}"
        )
    raw = matches[-1]
    return float("-inf") if raw == "-inf" else float(raw)


def analyze_file(path: Path, *, binary: str | None = None) -> LoudnessAnalysis:
    """Measure native loudness values for ``path`` with the shipped scanner.

    Resolves ffmpeg once and shares it across both passes: a PATH change
    between them would otherwise let the R128 and RMS measurements come from
    two different ffmpeg binaries. Callers that already resolved ffmpeg
    (the ``AnalyzerBackend`` wrapper) pass that executable in so R128, RMS
    and the record-metadata decode cannot drift.
    """
    binary = binary or require_ffmpeg()
    scan = scan_file(path, binary=binary)
    return LoudnessAnalysis(
        integrated_lufs=scan.integrated_lufs,
        true_peak_dbtp=scan.true_peak_dbtp,
        loudness_range_lu=scan.loudness_range_lu,
        rms_db=_scan_whole_file_rms(path, binary),
    )
