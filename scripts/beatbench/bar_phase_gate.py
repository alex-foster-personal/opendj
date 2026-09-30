"""Write the BEATMAP-01 bar-phase gate artifact from the committed round-1 raw arm.

Converts every subset90 row of `ops/beatbench/round-1/raw-beatgrid_lane_t050.json`
with the served lane builder (`lane_payload.build_beatgrid_lane`, raw mode) and
records what `tests/analysis_beatgrid/test_beatmap01_gate.py` checks: the ok
count and failure buckets, and the served bar-1 agreement with rekordbox on
the fixed-tempo ok rows. Round 3 wrote the same shape by hand; this is the
generator, so a later round re-derives the numbers instead of re-typing them.

The `*_min` floors are written equal to what was measured. Whether a lower
measured value may replace a higher floor is a review decision the PR makes
in words; this script only measures.

Usage:
  python -m scripts.beatbench.bar_phase_gate --out ops/beatbench/round-6/bar-phase-gate.json
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any

from apps.analysis_beatgrid.bar_phase import BAR_PHASE_AGREEMENT_FLOOR
from apps.analysis_beatgrid.flags import evaluate_pulse
from apps.analysis_beatgrid.lane_payload import build_beatgrid_lane
from apps.analysis_beatgrid.version import PRODUCER_VERSION
from apps.analysis_bench.scorers.beatgrid import (
    SCORER_VERSION,
    grid_is_dynamic,
    score_downbeats,
    score_positions,
)
from scripts.beatbench.report import window_slice

REPO = Path(__file__).resolve().parents[2]
RAW_PATH = REPO / "ops" / "beatbench" / "round-1" / "raw-beatgrid_lane_t050.json"
FIXTURES_PATH = REPO / "ops" / "beatbench" / "round-1" / "fixtures-subset90.json"


def _floor4(value: float) -> float:
    """Round DOWN to 4 places, so a floor written from a measure never exceeds it."""
    return math.floor(value * 10_000) / 10_000


def measure() -> dict[str, Any]:
    raw = json.loads(RAW_PATH.read_text(encoding="utf-8"))
    fixtures = json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))["fixtures"]
    threshold = float(raw["threshold"])
    rows = {Path(wav).stem: row for wav, row in raw["results"].items()}

    buckets: Counter[str] = Counter()
    part: dict[str, dict[str, list[float] | int]] = {
        name: {"n": 0, "n_ok": 0, "downbeat": [], "f": []} for name in ("fixed", "dynamic")
    }
    for fixture in fixtures:
        row = rows[fixture["stable_id"]]
        lane = build_beatgrid_lane(row, threshold=threshold)
        buckets["ok" if lane.ok else str(lane.reason).split(":")[0]] += 1
        cell = part["dynamic" if grid_is_dynamic(fixture["ref_beats"]) else "fixed"]
        cell["n"] += 1  # type: ignore[operator]

        start, end = fixture["score_start_s"], fixture["score_end_s"]
        offset = fixture["window_start_s"]
        ref_in = window_slice([b[1] for b in fixture["ref_beats"]], start, end)
        pulse = evaluate_pulse(
            row["beats"], row.get("activation_peak"), row.get("downbeats"), threshold=threshold
        )
        ungated = [] if pulse.no_trackable_pulse or row.get("error") else row["beats"]
        cand_in = window_slice([t + offset for t in ungated], start, end)
        cell["f"].append(score_positions(ref_in, cand_in).f_measure)  # type: ignore[union-attr]

        if not lane.ok:
            continue
        cell["n_ok"] += 1  # type: ignore[operator]
        ref_db = window_slice([b[1] for b in fixture["ref_beats"] if b[0] == 1], start, end)
        cand_db = window_slice(
            [b["t"] + offset for b in lane.payload["beats"] if b["n"] == 1], start, end
        )
        scored = score_downbeats(ref_db, cand_db)
        if scored.agreement is not None:
            cell["downbeat"].append(scored.agreement)  # type: ignore[union-attr]

    def _summary(cell: dict[str, Any], *, with_f: bool) -> dict[str, Any]:
        db, f = cell["downbeat"], cell["f"]
        out = {
            "n": cell["n"],
            "n_ok": cell["n_ok"],
            "n_downbeat_scored": len(db),
            "downbeat_mean": round(sum(db) / len(db), 4) if db else None,
        }
        if with_f:
            # Ungated F over every fixed row, the figure test_fixed_f_does_not_move pins.
            out["f_mean"] = round(sum(f) / len(f), 4) if f else None
        return out

    fixed = _summary(part["fixed"], with_f=True)
    dynamic = _summary(part["dynamic"], with_f=False)
    fixed_db: list[float] = part["fixed"]["downbeat"]  # type: ignore[assignment]
    fixed["downbeat_mean_min"] = _floor4(sum(fixed_db) / len(fixed_db))
    return {
        "producer_version": PRODUCER_VERSION,
        "scorer_version": SCORER_VERSION,
        "n_fixtures": len(fixtures),
        "n_ok": buckets["ok"],
        "n_failed": len(fixtures) - buckets["ok"],
        "n_ok_min": buckets["ok"],
        "buckets": dict(buckets.most_common()),
        "floor": BAR_PHASE_AGREEMENT_FLOOR,
        "fixed": fixed,
        "dynamic": dynamic,
        "source": {
            "raw": str(RAW_PATH.relative_to(REPO)),
            "fixtures": str(FIXTURES_PATH.relative_to(REPO)),
        },
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    result = measure()
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
