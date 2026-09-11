#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyloudnorm", "numpy"]
# ///
"""LUFS reference for the loudness lane, via pyloudnorm.

WHY THIS EXISTS. `apps/analysis_bench/scorers/loudness_lane.py` is the ruler
imported by pytest, and pyloudnorm must never enter the repo venv. Real-bundle
fixture build writes `lufs_pyloudnorm` from this script's output; synthetic
tests handwrite that field. The scorer only reads it.

THIS SCRIPT EMITS LUFS ONLY. pyloudnorm has no true-peak measurement. Spec
section 8: "if the dBTP gate compares against pyloudnorm (which has no true
peak) then broken". Decode matches `apps/analysis_loudness/round0.py` `_decode`
(ffmpeg f32le stereo at native rate) so a real bundle's LUFS truth is the
same quantity round 0 already measured.

NOT A TEST IN THE SUITE. pyloudnorm never enters the repo venv. Run by hand
or by a future fixture builder:

  uv run --no-sync --no-project --script scripts/loudnessbench/pyloudnorm_ref.py FILE [FILE ...]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from importlib.metadata import version as _pkg_version
from pathlib import Path

import numpy as np
import pyloudnorm  # type: ignore[import-not-found]  # PEP 723 dep, not in the repo venv


def _decode(path: Path) -> tuple[np.ndarray, int]:
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=sample_rate", "-of", "csv=p=0", str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0 or not probe.stdout.strip().isdigit():
        raise RuntimeError(f"ffprobe could not read sample rate for {path}: {probe.stderr}")
    sample_rate = int(probe.stdout.strip())
    decoded = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0",
            "-f", "f32le", "-ac", "2", "-ar", str(sample_rate), "-",
        ],
        capture_output=True,
        check=False,
    )
    if decoded.returncode != 0:
        raise RuntimeError(f"ffmpeg could not decode {path}: {decoded.stderr.decode()}")
    samples = np.frombuffer(decoded.stdout, dtype="<f4")
    if samples.size == 0 or samples.size % 2:
        raise RuntimeError(f"ffmpeg decoded no complete stereo frames for {path}")
    return samples.reshape(-1, 2), sample_rate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path, help="audio files to measure")
    args = parser.parse_args(argv)
    pkg_version = _pkg_version("pyloudnorm")
    rows = []
    for path in args.paths:
        samples, sample_rate = _decode(path)
        lufs = float(pyloudnorm.Meter(sample_rate).integrated_loudness(samples))
        rows.append(
            {
                "path": str(path),
                "integrated_lufs": lufs,
                "pyloudnorm_version": pkg_version,
            }
        )
    json.dump(rows, sys.stdout, indent=1)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
