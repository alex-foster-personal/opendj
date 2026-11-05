"""Control-position error between a panel layout and a reference (skins strategy, hardware veto).

Both files use the panel layout format (``opendj-panel-layout/1``): ``panel.w`` plus a
``controls`` list of ``{id, x, y, w, h}``. Each side is scaled so its panel is 1000 px wide,
then the distance between matching control centers is measured.

Verdicts (exit code):
  0 PASS     every candidate control is matched and p95 <= 8 px and max <= 15 px
  1 FAIL     a threshold is exceeded, or a candidate control has no reference row
  3 UNKNOWN  the reference declares ``"measured": false`` (a schematic is not a measurement),
             or no control could be matched at all

Usage:
  uv run --locked python -m scripts.skins.control_position_error CANDIDATE REFERENCE [--json]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

P95_MAX_PX = 8.0
MAX_PX = 15.0
PLATE_PX = 1000.0
EXIT_PASS, EXIT_FAIL, EXIT_UNKNOWN = 0, 1, 3


@dataclass(frozen=True)
class Report:
    verdict: str
    matched: int
    candidate_total: int
    unmatched: list[str]
    p50_px: float | None
    p95_px: float | None
    max_px: float | None
    worst: list[tuple[str, float]]
    reason: str


# ----------------------------------------------------------------- helpers


def _centers(doc: dict) -> dict[str, tuple[float, float]]:
    scale = PLATE_PX / float(doc["panel"]["w"])
    return {c["id"]: ((c["x"] + c["w"] / 2) * scale, (c["y"] + c["h"] / 2) * scale) for c in doc["controls"]}


def _percentile(sorted_vals: list[float], pct: float) -> float:
    k = (len(sorted_vals) - 1) * pct
    lo, hi = math.floor(k), math.ceil(k)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


# ----------------------------------------------------------------- core


def measure(candidate: dict, reference: dict) -> Report:
    cand, ref = _centers(candidate), _centers(reference)
    errors = {cid: math.dist(xy, ref[cid]) for cid, xy in cand.items() if cid in ref}
    unmatched = sorted(set(cand) - set(ref))
    if not errors:
        return Report("UNKNOWN", 0, len(cand), unmatched, None, None, None, [], "no control matched")
    vals = sorted(errors.values())
    p50, p95, mx = _percentile(vals, 0.5), _percentile(vals, 0.95), vals[-1]
    worst = sorted(errors.items(), key=lambda kv: -kv[1])[:5]
    if reference.get("measured") is False:
        verdict, reason = "UNKNOWN", "reference is a schematic, not a measurement"
    elif unmatched:
        verdict, reason = "FAIL", f"{len(unmatched)} candidate controls missing from the reference"
    elif p95 > P95_MAX_PX or mx > MAX_PX:
        verdict, reason = "FAIL", f"p95 {p95:.1f} px (limit {P95_MAX_PX}) / max {mx:.1f} px (limit {MAX_PX})"
    else:
        verdict, reason = "PASS", "within thresholds"
    return Report(
        verdict,
        len(errors),
        len(cand),
        unmatched,
        round(p50, 2),
        round(p95, 2),
        round(mx, 2),
        [(k, round(v, 2)) for k, v in worst],
        reason,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("candidate", type=Path)
    parser.add_argument("reference", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = measure(json.loads(args.candidate.read_text()), json.loads(args.reference.read_text()))
    if args.json:
        print(json.dumps(asdict(report), indent=1))
    else:
        print(
            f"[{report.verdict}] {report.reason}: matched {report.matched}/{report.candidate_total}, "
            f"p50 {report.p50_px} px, p95 {report.p95_px} px, max {report.max_px} px; worst {report.worst}"
        )
    return {"PASS": EXIT_PASS, "FAIL": EXIT_FAIL}.get(report.verdict, EXIT_UNKNOWN)


if __name__ == "__main__":
    sys.exit(main())
