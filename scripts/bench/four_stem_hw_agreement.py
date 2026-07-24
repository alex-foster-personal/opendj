# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Does the CARD change per-stem SI-SDR, or only the clock?

The n=1 four-stem result this project is re-testing ran on an M3 CPU, and "that
was not the production GPU" is a standing reason to distrust it. That objection
is about to decide a model default, so it gets measured rather than argued away.

SI-SDR is a property of the separation, not of the silicon, so the EXPECTATION is
agreement well inside this project's 0.2 dB audibility bar. Expectation is not
evidence.

TWO INDEPENDENT CPU SOURCES, and the first one is free:

  1. scripts/bench/ladder_4stem_all.json -- the COMMITTED M3-CPU artifact under
     test. Comparing the H100 run against it needs no new spend and is the
     strongest form of the check, because it is the exact artifact whose
     hardware is in question.
  2. .tmp/bench/4stem-matrix/cells.jsonl -- the local CPU re-run of the same
     matrix, if it has finished any cells. Wider coverage, same windows.

  [if] every shared cell agrees within TOL_DB [then] the card is a cost question
       only and either cube can carry the quality verdict
  [if] any does not [then ⛔️] exit 1: a model default cannot be settled on one
       kind of hardware and shipped on another

Run:
  uv run scripts/bench/four_stem_hw_agreement.py

-Claude
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path
from typing import Any

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
GPU_JSON: Path = REPO_ROOT / "scripts/bench/four_stem_gpu_matrix.json"
LADDER_JSON: Path = REPO_ROOT / "scripts/bench/ladder_4stem_all.json"
CPU_CELLS: Path = REPO_ROOT / ".tmp/bench/4stem-matrix/cells.jsonl"
OUT_JSON: Path = REPO_ROOT / "scripts/bench/four_stem_hw_agreement.json"

STEMS: tuple[str, ...] = ("vocals", "drums", "bass", "other")
# A quarter of the 0.2 dB audibility bar. A difference under this cannot flip a
# per-stem verdict, which is the only thing the comparison has to protect.
TOL_DB: float = 0.05
# ladder_4stem_all arm ids that share this run's knobs (overlap 0.25, 44.1 kHz).
LADDER_ARMS: dict[str, str] = {
    "hdemucs_mmi": "hdemucs_mmi-ov25", "htdemucs": "htdemucs-ov25"}


def _rows_from_ladder(gpu_cube: dict[str, Any]) -> list[dict[str, Any]]:
    ladder = json.loads(LADDER_JSON.read_text())
    track = ladder["track"].split(" (")[0]
    if track not in gpu_cube:
        raise SystemExit(f"error: {track!r} is not in the GPU cube; cannot compare")
    arms = {a["id"]: a for a in ladder["arms"]}
    rows = []
    for model, arm_id in LADDER_ARMS.items():
        for stem in STEMS:
            cpu = arms[arm_id]["stems"][stem]["si_sdr"]
            gpu = gpu_cube[track][model][stem]
            rows.append({
                "source": "ladder_4stem_all.json (committed, Apple M3 CPU)",
                "track": track, "model": model, "stem": stem,
                "cpu_si_sdr": cpu, "gpu_si_sdr": gpu,
                "gpu_minus_cpu_db": round(gpu - cpu, 3),
                "agrees": abs(gpu - cpu) <= TOL_DB,
            })
    return rows


def _rows_from_cells(gpu_cube: dict[str, Any]) -> list[dict[str, Any]]:
    if not CPU_CELLS.is_file():
        return []
    rows = []
    for line in CPU_CELLS.read_text().splitlines():
        if not line.strip():
            continue
        cell = json.loads(line)
        if cell.get("device") != "cpu":
            continue
        track, model = cell["track"], cell["model"]
        if track not in gpu_cube or model not in gpu_cube[track]:
            continue
        for stem in STEMS:
            cpu = cell["si_sdr"][stem]
            gpu = gpu_cube[track][model][stem]
            rows.append({
                "source": ".tmp/bench/4stem-matrix/cells.jsonl (local CPU re-run)",
                "track": track, "model": model, "stem": stem,
                "cpu_si_sdr": cpu, "gpu_si_sdr": gpu,
                "gpu_minus_cpu_db": round(gpu - cpu, 3),
                "agrees": abs(gpu - cpu) <= TOL_DB,
            })
    return rows


def main() -> int:
    gpu = json.loads(GPU_JSON.read_text())
    cube = gpu["si_sdr"]
    rows = _rows_from_ladder(cube) + _rows_from_cells(cube)
    if not rows:
        raise SystemExit("error: no shared cells to compare; nothing was checked")

    disagreements = [r for r in rows if not r["agrees"]]
    worst = max(rows, key=lambda r: abs(r["gpu_minus_cpu_db"]))
    by_source: dict[str, int] = {}
    for row in rows:
        by_source[row["source"]] = by_source.get(row["source"], 0) + 1

    payload = {
        "question": "does the production card change per-stem SI-SDR, or only the clock?",
        "gpu": gpu["hardware"],
        "tolerance_db": TOL_DB,
        "cells_compared": len(rows),
        "cells_by_source": by_source,
        "n_disagreements": len(disagreements),
        "max_abs_gpu_minus_cpu_db": abs(worst["gpu_minus_cpu_db"]),
        "mean_abs_gpu_minus_cpu_db": round(
            statistics.fmean(abs(r["gpu_minus_cpu_db"]) for r in rows), 4),
        "worst_cell": worst,
        "verdict": (
            "AGREE: the card changes the clock, not the quality, so the GPU cube "
            "carries the per-stem verdict"
            if not disagreements else
            "DISAGREE: a model default cannot be settled on one kind of hardware "
            "and shipped on another"),
        "cells": rows,
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    for source, count in by_source.items():
        print(f"  {count:3d} cells from {source}")
    print(f"\n{len(rows) - len(disagreements)}/{len(rows)} cells agree within {TOL_DB} dB")
    print(f"worst |delta| {payload['max_abs_gpu_minus_cpu_db']} dB "
          f"({worst['track']} / {worst['model']} / {worst['stem']}), "
          f"mean |delta| {payload['mean_abs_gpu_minus_cpu_db']} dB")
    print(f"[OK] wrote {OUT_JSON.relative_to(REPO_ROOT)}")
    if disagreements:
        print("\n[ERROR] disagreements:", file=sys.stderr)
        for row in disagreements[:20]:
            print(f"  {row['track']}/{row['model']}/{row['stem']}: "
                  f"cpu {row['cpu_si_sdr']} gpu {row['gpu_si_sdr']}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
