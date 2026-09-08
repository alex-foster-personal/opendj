#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11,<3.13"
# dependencies = ["fast-bss-eval>=0.1.4", "numpy<2", "soundfile>=0.12"]
# ///
"""Reproducible, foreground benchmark for issue #1461 local-stems candidates.

This research harness exports N real MUSDB18 seven-second clips, then runs each
candidate once per clip with a shell ``timeout``. It writes its JSON ledger
after every candidate/clip cell, so a failed download or a stopped run leaves
the completed measurements intact. The fixture is intentionally only 6.803 s;
these are clip-length results, never full-track claims.

Quality uses the existing TRUE-stem SI-SDR convention and the calibrated
``scripts.bench.four_stem_common.AUDIBILITY_DB`` (0.2 dB) stem-tier bar. A
candidate difference under that bar is output parity for this spike. The
existing stem-content gate is also recorded for every fixture, so a quiet true
stem cannot silently drive the comparison.

Run:
  uv run --with 'fast-bss-eval>=0.1.4' --with 'numpy<2' --with 'soundfile>=0.12' \
      python -m scripts.bench.local.run_local_stems_benchmark --n 2

The command auto-detects MPS/CUDA/CPU inside each candidate. On this nucbox it
therefore records an x86 CPU baseline; on the maintainer's Air, the identical command
records an MPS row. It never fabricates a CoreML or MPS row.

-Claude
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.bench.four_stem_common import AUDIBILITY_DB, STEMS

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CACHE = REPO_ROOT / ".tmp" / "local-stems-bench"


@dataclass(frozen=True)
class Candidate:
    key: str
    script: Path


CANDIDATES: tuple[Candidate, ...] = (
    Candidate("htdemucs_torch", Path("scripts/bench/local/candidate_htdemucs_cpu.py")),
    Candidate("torchaudio_hybrid_demucs", Path("scripts/bench/local/candidate_torchaudio_hybrid_cpu.py")),
)


def _parse_candidate_json(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        if line.startswith("{"):
            parsed = json.loads(line)
            if not isinstance(parsed, dict):
                break
            return parsed
    raise RuntimeError("candidate exited successfully but emitted no JSON result")


def output_parity(reference_db: float, challenger_db: float) -> str:
    return (
        "within-audibility-bar"
        if abs(challenger_db - reference_db) < AUDIBILITY_DB
        else "material-difference"
    )


def _load_mono(path: Path) -> tuple[Any, int]:
    import soundfile as sf

    audio, sample_rate = sf.read(path, dtype="float64", always_2d=True)
    return audio.mean(axis=1), int(sample_rate)


def _score_outputs(track_dir: Path, output_dir: Path) -> dict[str, Any]:
    import numpy as np

    from scripts.bench.separation_metrics import si_sdr_db

    scores: dict[str, float] = {}
    for stem in STEMS:
        truth, truth_rate = _load_mono(track_dir / f"{stem}.wav")
        estimate, estimate_rate = _load_mono(output_dir / f"{stem}.wav")
        if truth_rate != estimate_rate:
            raise RuntimeError(
                f"{stem}: output rate {estimate_rate} differs from truth rate {truth_rate}; refusing resample"
            )
        scores[stem] = si_sdr_db(truth, estimate)
    return {
        "per_stem_si_sdr_db": scores,
        "mean_si_sdr_db": round(float(np.mean(list(scores.values()))), 3),
    }


def _fixture_content(track_dir: Path) -> dict[str, Any]:
    import soundfile as sf
    from scripts.bench.stem_content_gate import measure_window

    mixture, rate = sf.read(track_dir / "mixture.wav", always_2d=True)
    contents, floors = measure_window(track_dir, 0, mixture.shape[0])
    return {
        "duration_s": round(mixture.shape[0] / rate, 3),
        "truth_stem_failures": {content.stem: content.failures() for content in contents},
        "mixture_floor_si_sdr_db": floors,
    }


def _run(command: list[str], timeout_s: int) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    # The harness itself runs in an ephemeral ``uv --with`` environment. Let
    # each PEP 723 child resolve its own pinned environment instead of inheriting
    # that virtualenv marker and waiting on an incompatible environment lock.
    environment.pop("VIRTUAL_ENV", None)
    return subprocess.run(
        ["timeout", f"{timeout_s}s", *command],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )


def _write_ledger(path: Path, ledger: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, indent=2) + "\n", encoding="utf-8")


def _candidate_cell(candidate: Candidate, track_dir: Path, out_dir: Path, timeout_s: int) -> dict[str, Any]:
    run = _run(
        ["uv", "run", str(candidate.script), "--mixture", str(track_dir / "mixture.wav"), "--out", str(out_dir)],
        timeout_s,
    )
    cell: dict[str, Any] = {
        "candidate": candidate.key,
        "track": track_dir.name,
        "timeout_s": timeout_s,
        "exit_code": run.returncode,
        "stderr": run.stderr.strip(),
    }
    if run.returncode != 0:
        cell["status"] = "failed" if run.returncode != 124 else "timed-out"
        cell["stdout"] = run.stdout.strip()
        return cell
    candidate_result = _parse_candidate_json(run.stdout)
    if candidate_result.get("device") not in {"cpu", "cuda", "mps"}:
        raise RuntimeError(f"{candidate.key} did not report a valid device: {candidate_result}")
    cell.update({"status": "measured", "machine_result": candidate_result})
    cell.update(_score_outputs(track_dir, out_dir))
    return cell


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--n", type=int, default=2, help="number of 6.803-second clips")
    parser.add_argument("--timeout-s", type=int, default=600, help="per fixture/candidate bound")
    parser.add_argument("--ledger", type=Path, default=None)
    args = parser.parse_args()
    if args.n < 1:
        raise ValueError(f"--n must be positive, got {args.n}")
    if args.timeout_s < 1:
        raise ValueError(f"--timeout-s must be positive, got {args.timeout_s}")

    fixture_dir = args.cache / "fixtures"
    ledger_path = args.ledger or args.cache / "ledger.json"
    manifest_path = fixture_dir / "manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if len(manifest.get("tracks", [])) < args.n:
            raise RuntimeError(
                f"cached fixture manifest has {len(manifest.get('tracks', []))} tracks, need {args.n}; "
                "delete the cache explicitly before asking this harness to fetch a larger sample"
            )
        manifest["tracks"] = manifest["tracks"][: args.n]
    else:
        fixtures = _run(
            ["uv", "run", "scripts/bench/local/fixtures.py", "--out", str(fixture_dir), "--n", str(args.n)],
            args.timeout_s,
        )
        if fixtures.returncode != 0:
            raise RuntimeError(
                f"fixture export failed with exit {fixtures.returncode}: {fixtures.stderr.strip() or fixtures.stdout.strip()}"
            )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    ledger: dict[str, Any] = {
        "issue": 1461,
        "machine": {
            "node": platform.node(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "fixture_source": manifest["source"],
        "clip_length_note": "Each fixture is approximately 6.803 seconds; no result extrapolates to a full track.",
        "quality_measure": {
            "metric": "true-stem SI-SDR per stem, mean across four stems",
            "parity_bar_db": AUDIBILITY_DB,
            "source": "scripts/bench/four_stem_common.py:AUDIBILITY_DB",
        },
        "cells": [],
    }
    for track in manifest["tracks"]:
        track_dir = Path(track["dir"])
        fixture = _fixture_content(track_dir)
        for candidate in CANDIDATES:
            cell = _candidate_cell(candidate, track_dir, args.cache / "output" / candidate.key / track_dir.name, args.timeout_s)
            cell["fixture"] = fixture
            ledger["cells"].append(cell)
            _write_ledger(ledger_path, ledger)
            print(json.dumps(cell), flush=True)


if __name__ == "__main__":
    main()
