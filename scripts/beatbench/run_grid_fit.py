"""Grid-fit candidates from an existing beat_this candidate, no model run needed.

Reads a scored round's committed Beat This! beats (round 0 by default: 337
fixtures, the same model and weights the lane serves) and writes one harness
candidate per grid-fit variant, so `scripts/beatbench/report.py` scores them
side by side against rekordbox:

  served_raw         what the lane serves today: raw model beats, bar numbers
                     from `bar_phase.lock_bar_phase`, failures as failures.
  line               `grid_fit.fit_grid` with no rounding and no offset.
  line_round         plus DJ-software BPM rounding.
  line_round_offset  plus the served offset (`grid_design.OFFSET_SERVED_S`,
                     +15 ms, matches rekordbox): exactly what the lane serves
                     with MDT_BEATGRID_GRID_FIT=line.
  line_round_offset_v2_target
                     the same with the v2 target offset (+8 ms, human truth).
  line_round_offset_octave
                     EXPERIMENT, not served: the grid re-rendered at the octave
                     `bpm.estimate_bpm` publishes. Measured to halve 163-175 BPM
                     tracks rekordbox keeps whole, so the lane does not do it.
  const_regions_only_round_offset
                     `const_regions.fit_const_regions` as the recipe stands:
                     25 ms regions, the longest of 16+ beats extended over
                     every neighbor that lands on it, rounded, phase averaged,
                     served offset. A track with no such region FAILS here.
  const_regions_round
                     the served constant-region fit, no offset: the recipe
                     where its span covers `MIN_COVERAGE` (0.5) of the track,
                     the line fitter elsewhere.
  const_regions_round_offset
                     plus the served offset: exactly what the lane serves with
                     MDT_BEATGRID_GRID_FIT=const_regions.
  const_regions_cov70_round_offset, const_regions_cov90_round_offset
                     the coverage gate at 0.7 and 0.9, to show where 0.5 sits.
  const_regions_piecewise_round_offset
                     EXPERIMENT, not served: every long region keeps its own
                     line, toward piecewise grids (#1481). No fallback.
  const_regions_lsq_only_round_offset, const_regions_lsq_round_offset
                     each region's line fitted by least squares over its beats
                     rather than drawn through its end beats, alone and served
                     (see `const_regions.find_const_regions`).

Every variant sees the same input beats, so the deltas between rows are the
fit alone. A variant that cannot grid a track records an error for it rather
than dropping the row, so no variant shrinks its own denominator.

Usage:
  python -m scripts.beatbench.run_grid_fit \\
      --candidate ops/beatbench/round-0/candidate-beat_this.json \\
      --out-dir ops/beatbench/round-4
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import time
from collections.abc import Callable
from typing import Any

from apps.analysis_beatgrid.bar_phase import lock_bar_phase
from apps.analysis_beatgrid.bpm import estimate_bpm
from apps.analysis_beatgrid.const_regions import MIN_COVERAGE, fit_const_regions
from apps.analysis_beatgrid.grid_design import OFFSET_V2_TARGET_S
from apps.analysis_beatgrid.grid_fit import DEFAULT_OFFSET_S, fit_grid

VARIANTS: dict[str, dict[str, Any]] = {
    "line": {"rounding": False, "offset_s": 0.0},
    "line_round": {"rounding": True, "offset_s": 0.0},
    "line_round_offset": {"rounding": True, "offset_s": DEFAULT_OFFSET_S},
    "line_round_offset_v2_target": {"rounding": True, "offset_s": OFFSET_V2_TARGET_S},
    "line_round_offset_octave": {"rounding": True, "offset_s": DEFAULT_OFFSET_S, "octave": True},
    "const_regions_lsq_only_round_offset": {
        "rounding": True,
        "offset_s": DEFAULT_OFFSET_S,
        "method": "const",
        "fallback_line": False,
        "min_coverage": 0.0,
        "least_squares": True,
    },
    "const_regions_lsq_round_offset": {
        "rounding": True,
        "offset_s": DEFAULT_OFFSET_S,
        "method": "const",
        "least_squares": True,
    },
    "const_regions_only_round_offset": {
        "rounding": True,
        "offset_s": DEFAULT_OFFSET_S,
        "method": "const",
        "fallback_line": False,
        "min_coverage": 0.0,
    },
    "const_regions_round": {"rounding": True, "offset_s": 0.0, "method": "const"},
    "const_regions_round_offset": {
        "rounding": True,
        "offset_s": DEFAULT_OFFSET_S,
        "method": "const",
    },
    "const_regions_cov70_round_offset": {
        "rounding": True,
        "offset_s": DEFAULT_OFFSET_S,
        "method": "const",
        "min_coverage": 0.7,
    },
    "const_regions_cov90_round_offset": {
        "rounding": True,
        "offset_s": DEFAULT_OFFSET_S,
        "method": "const",
        "min_coverage": 0.9,
    },
    "const_regions_piecewise_round_offset": {
        "rounding": True,
        "offset_s": DEFAULT_OFFSET_S,
        "method": "piecewise",
        "fallback_line": False,
    },
}


def _failed(reason: str) -> dict[str, Any]:
    return {"beats": [], "downbeats": [], "native_bpm": None, "error": reason}


def served_raw(row: dict[str, Any]) -> dict[str, Any]:
    beats = row.get("beats") or []
    lock = lock_bar_phase(beats, row.get("downbeats") or [])
    if lock.bar_phase_unestablished:
        return _failed(str(lock.reason))
    return {
        "beats": beats,
        "downbeats": [t for t, n in zip(beats, lock.beat_numbers, strict=True) if n == 1],
        "native_bpm": None,
        "error": None,
    }


def fitted(
    row: dict[str, Any],
    *,
    rounding: bool,
    offset_s: float,
    octave: bool = False,
    method: str = "line",
    fallback_line: bool = True,
    min_coverage: float = MIN_COVERAGE,
    least_squares: bool = False,
) -> dict[str, Any]:
    if row.get("error"):
        return _failed(str(row["error"]))
    beats = row.get("beats") or []
    multiple = 1.0
    if octave:
        tempo = estimate_bpm(beats)
        if tempo is None:
            return _failed("no_tempo_fit")
        multiple = tempo.octave_multiple
    if method == "line":
        fit = fit_grid(
            beats,
            row.get("downbeats") or [],
            rounding=rounding,
            offset_s=offset_s,
            octave_multiple=multiple,
        )
    else:
        fit = fit_const_regions(
            beats,
            row.get("downbeats") or [],
            rounding=rounding,
            offset_s=offset_s,
            min_coverage=min_coverage,
            fallback_line=fallback_line,
            piecewise=method == "piecewise",
            least_squares=least_squares,
        )
    if fit.reason:
        return _failed(fit.reason)
    return {
        "beats": fit.beats,
        "downbeats": [t for t, n in zip(fit.beats, fit.beat_numbers, strict=True) if n == 1],
        "native_bpm": fit.lines[0].bpm if len(fit.lines) == 1 else None,
        "n_segments": len(fit.lines),
        "round_steps": [ln.round_step for ln in fit.lines],
        "fitter": fit.fitter,
        "error": None,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--candidate", required=True, help="a beat_this candidate JSON with raw beats")
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args(argv)

    with open(args.candidate, encoding="utf-8") as fh:
        source = json.load(fh)
    rows: dict[str, dict[str, Any]] = source["results"]
    os.makedirs(args.out_dir, exist_ok=True)

    builders: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {"served_raw": served_raw}
    for name, kw in VARIANTS.items():
        builders[name] = functools.partial(fitted, **kw)

    for name, build in builders.items():
        results = {sid: build(row) for sid, row in rows.items()}
        payload = {
            "schema": 1,
            "candidate": name,
            "candidate_version": (
                f"grid_fit over {source['candidate']} ({source['candidate_version']})"
            ),
            "source_candidate": os.path.relpath(args.candidate),
            "license": source["license"],
            "shippable": True,
            "emits_downbeats": True,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "workers": 1,
            "n_fixtures": len(results),
            "n_failed": sum(1 for r in results.values() if r["error"]),
            "results": results,
        }
        path = os.path.join(args.out_dir, f"candidate-{name}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        print(f"[grid_fit] {name}: {payload['n_failed']} failed of {len(results)} -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
