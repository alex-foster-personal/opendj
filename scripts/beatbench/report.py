"""Score every candidate against the rekordbox grids and write the round table.

RUN WITH THE REPO VENV, not as a PEP 723 script: this is the one piece that
imports the scorer, and the scorer must be the same code the acceptance tests
pin. The analyzers live in throwaway environments; the ruler does not.

WHAT THIS DECIDES, AND WHY IT IS CENTRALISED HERE.

Candidates emit beat times relative to their excerpt and nothing else. Every
interpretation happens here, once, identically for all of them:

  - Excerpt-relative times are shifted into absolute track time.
  - Both grids are cut to the SCORING window, which is the excerpt inset by a
    guard band at each end. Beat trackers are unreliable in their first and
    last moments, and that is an artifact of excerpting rather than a property
    of the analyzer, so it is trimmed for everyone rather than for nobody.
  - Candidate tempo is DERIVED from beat spacing rather than taken from
    whatever the analyzer volunteered. Some candidates report a native BPM and
    some do not, and the ones that do use different definitions. Deriving it
    the same way for everyone is the only comparison that means anything. The
    native figure is still carried through to the JSON for reference.

TWO BPM REFERENCES, BECAUSE REKORDBOX HAS TWO. The stored djmdContent BPM is
the single number rekordbox displays and the one a user would call "the BPM".
The grid's own local tempo inside the window is what the beats actually do. On
a fixed grid these are the same number. On a dynamic grid they are not, and the
stored value is close to meaningless, so both are reported and the dynamic
split is read against the local one.

FIXED AND DYNAMIC ARE NEVER BLENDED INTO ONE HEADLINE. The fixture set takes
every dynamic track and only a sample of fixed ones, so a raw average over the
fixtures would badly overweight dynamic tracks. Any library-wide figure is
reweighted by the real population counts, and it is labelled as such.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from apps.analysis_bench.scorers.beatgrid import BEAT_TOLERANCE_S, SCORER_VERSION, window_slice
from apps.analysis_bench.scorers.beatgrid_report import (
    _unit,
    aggregate,
    eval_partition_for_fixtures,
    load_fixtures,
    render_table,
    score_track,
)

# Scoring functions live in apps.analysis_bench.scorers.beatgrid_report.
# Re-exported here so this CLI module's public names stay put.
__all__ = [
    "BEAT_TOLERANCE_S",
    "SCORER_VERSION",
    "aggregate",
    "eval_partition_for_fixtures",
    "load_fixtures",
    "main",
    "render_report",
    "render_table",
    "score_track",
    "weight_cells",
    "window_slice",
]


def weight_cells(fixed: dict, dynamic: dict, w_fixed: int, w_dynamic: int) -> dict[str, Any]:
    """Reweight the two cells by the real library population, not the sample."""
    if not fixed.get("n") or not dynamic.get("n"):
        return {"n": 0}
    total = w_fixed + w_dynamic
    out: dict[str, Any] = {"basis": f"weighted {w_fixed} fixed : {w_dynamic} dynamic"}
    for key, value in fixed.items():
        other = dynamic.get(key)
        if isinstance(value, (int, float)) and isinstance(other, (int, float)) and key != "n":
            out[key] = (value * w_fixed + other * w_dynamic) / total
    return out


def render_report(payload: dict[str, Any]) -> str:
    man = payload["manifest"]
    den = man["denominator"]
    pop = man["population"]
    lines = [
        f"# Beat-mapping benchmark, round {payload['round']}",
        "",
        f"Generated {payload['generated_at']} with scorer v{payload['scorer_version']}, "
        f"beat tolerance {int(BEAT_TOLERANCE_S * 1000)} ms.",
        "",
        "Ground truth is rekordbox's own ANLZ PQTZ beat grid, read through the lane daemon. "
        "The KPI is agreement with rekordbox, so every figure below is a distance from what "
        "rekordbox already believes, not an independent judgement of correctness.",
        "",
        "> **Scorer v1.1.0 changed what three metrics MEAN**, so a figure here is "
        "comparable with round 0 ONLY against round-0 artifacts rescored at v1.1.0, "
        "never against the numbers printed in the round-0 report. Candidate tempo is "
        "now a least-squares fit of beat time against beat index rather than the "
        "median inter-beat interval, which quantization biased; CMLt and AMLt are new; "
        "and F is reported shift-corrected alongside raw. See "
        "(private path).",
        "",
        "## Denominator",
        "",
        "| bucket | rows |",
        "|---|---|",
        f"| library rows total | {den['rows_total']} |",
        f"| audio present on disk | {den['rows_audio_present']} |",
        f"| surveyed for a grid | {den['rows_surveyed']} |",
        f"| rekordbox grid present | {den['rows_with_rbx_grid']} |",
        f"| grid fixed tempo | {den['rows_grid_fixed']} |",
        f"| grid dynamic tempo | {den['rows_grid_dynamic']} |",
        f"| no grid in ANLZ | {den['rows_no_grid']} |",
        f"| ANLZ read error | {den['rows_anlz_error']} |",
        "",
        f"Fixture-eligible after the {man['excerpt']['min_duration_s']:.0f}s minimum duration "
        f"rule: "
        f"{pop['eligible_fixed']} fixed, {pop['eligible_dynamic']} dynamic. "
        f"Built {man['selection']['built_fixed']} fixed and {man['selection']['built_dynamic']} "
        f"dynamic fixtures, dropping {man['selection']['dropped']}.",
        "",
        "EVERY RATE BELOW IS AGAINST THE FIXTURE COUNT IN ITS OWN CELL, never the library. "
        "Dynamic grids are fully enumerated while fixed grids are sampled, so the fixture set "
        "is deliberately not proportional to the library and the two cells must not be averaged "
        "naively. The weighted row reweights them by the real population.",
        "",
        f"Excerpt: {man['excerpt']['length_s']:.0f}s from {man['excerpt']['start_fraction']:.0%} "
        f"into each track, decoded once to {man['excerpt']['sample_rate']} Hz mono so every "
        f"candidate reads identical bytes, scored with a {man['excerpt']['guard_s']:.0f}s guard "
        "band trimmed from each end.",
        "",
        f"## Round-{payload['round']} table",
        "",
    ]
    eval_part = payload.get("eval_partition") or {}
    if eval_part:
        lines += [
            f"Partition at evaluation (`{eval_part.get('predicate', 'grid_is_dynamic')}` "
            f"over reference beats scored): **{eval_part.get('n_fixed', 0)} fixed**, "
            f"**{eval_part.get('n_dynamic', 0)} dynamic**. "
            "Survey denominators above are the Wed 19 Aug 2026 library snapshot; "
            "these cell `n` values are the re-count for this fixture set.",
            "",
        ]

    cells_fixed = [(c["label"], c["fixed"], c) for c in payload["candidates"]]
    cells_dyn = [(c["label"], c["dynamic"], c) for c in payload["candidates"]]
    lines += render_table("Fixed-tempo rekordbox grids", cells_fixed)
    lines += render_table("Dynamic-tempo rekordbox grids", cells_dyn)

    lines += [
        "Columns: BPM bands are agreement with the STORED rekordbox BPM at 0.01 (its storage "
        "granularity, since PQTZ tempo is an integer of BPM x100), 0.1 and 1.0. half and double "
        "are octave errors, counted separately from misses because choosing the wrong metrical "
        "level is a different failure from losing the pulse. F@70ms is one-to-one beat agreement "
        "at the MIR-standard tolerance. raw p50/p95 are nearest-beat offsets in ms; shift p50/p95 "
        "are the same after removing the MEDIAN signed offset, a lower bound on the best "
        "achievable shift rather than the F-maximizing one, so raw-minus-shifted is constant "
        "phase error and shifted alone is jitter. downbeat is agreement with rekordbox bar-1 "
        "beats, N/A where the analyzer has no downbeat concept. F shifted is the F-measure "
        "after that same shift removal, so F versus F-shifted separates a wrong grid from a "
        "right grid at the wrong phase. CMLt and AMLt are the MIR continuity metrics at the "
        "standard 17.5 percent phase and period tolerances: CMLt counts beats that are "
        "correct in BOTH phase and local tempo, and AMLt allows the four metrical "
        "variations (double, off-beat, and the two half-tempo phases). AMLt minus CMLt is "
        "therefore CONTINUITY RECOVERED BY THOSE ALLOWED VARIANTS, not a count or rate of "
        "tracks with octave errors: both terms are fractional continuity scores, and the "
        "off-beat variant means part of any gap is ordinary phase recovery rather than a "
        "metrical-level mistake. Read it as an upper bound on level-and-phase disagreement, "
        "and use the half and double columns for an actual classified octave rate. "
        "cont. n is how many of the cell's tracks had enough "
        "beats on both sides to score continuity at all, which is the denominator those two "
        "columns divide by, NOT the cell's n.",
        "",
        "## Runtime",
        "",
        "| candidate | serial per-track p50 | serial realtime factor | parallel wall | workers | est. full corpus |",
        "|---|---|---|---|---|---|",
    ]
    for cand in payload["candidates"]:
        rt = cand["runtime"]
        lines.append(
            f"| {cand['label']} | {_unit(rt.get('serial_p50_s'), '.2f', 's')} | "
            f"{_unit(rt.get('serial_realtime_factor'), '.1f', 'x')} | "
            f"{_unit(rt.get('parallel_wall_s'), '.0f', 's')} | "
            f"{rt.get('parallel_workers', 'n/a')} | "
            f"{rt.get('full_corpus_estimate', 'n/a')} |"
        )
    lines += [
        "",
        "A SERIAL FIGURE IS ONLY A MEASUREMENT ON A QUIET HOST, and `n/a` in these columns "
        "means NOT MEASURED rather than zero or instant. Serial figures are meant to be taken "
        "one candidate at a time, single worker, nothing else running; that is the only "
        "condition under which they are the honest per-track cost. Round 0 measured contention "
        "understating librosa by roughly 85x when five candidates ran concurrently, so a figure "
        "from a loaded host is not a slower measurement, it is not a measurement. The round's "
        "entry in (private path) is authoritative on the load actually carried. "
        "Parallel wall is the same pass with several workers, useful only for planning batch "
        "duration, under the same caveat.",
    ]
    # Stated only when TRUE of this round: a shared renderer must not hardcode
    # one round's circumstances.
    if all(c["runtime"].get("serial_realtime_factor") is None for c in payload["candidates"]):
        lines += [
            "",
            "**This round published no serial figure and therefore no full-corpus estimate.** "
            "Deliberate, not an omission: an estimate from a contended run would mis-size the "
            "backfill while carrying the authority of a measured number. Re-run the serial "
            "pass on a quiet host to fill these columns.",
        ]
    lines += [
        "",
        "## Licensing, which constrains the answer as much as accuracy does",
        "",
        "| candidate | licence | shippable |",
        "|---|---|---|",
    ]
    for cand in payload["candidates"]:
        lines.append(f"| {cand['label']} | {cand['license']} | {'yes' if cand['shippable'] else 'NO'} |")
    lines.append("")
    return "\n".join(lines)


# ----- Entry point --------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fixtures", required=True)
    ap.add_argument("--full", nargs="+", required=True, help="full-pass candidate JSONs")
    ap.add_argument("--serial", nargs="*", default=[], help="serial calibration JSONs")
    ap.add_argument("--out-md", required=True)
    ap.add_argument("--out-json", required=True)
    ap.add_argument("--corpus-tracks", type=int, default=10000)
    ap.add_argument("--round", type=int, required=True, help="experiment round this table is")
    args = ap.parse_args(argv)

    manifest, fixtures = load_fixtures(args.fixtures)

    serial_by_name: dict[str, dict] = {}
    for path in args.serial:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        serial_by_name[data["candidate"]] = data

    candidates: list[dict[str, Any]] = []
    for path in args.full:
        with open(path, encoding="utf-8") as fh:
            full = json.load(fh)
        rows = [
            scored
            for sid, result in full["results"].items()
            if sid in fixtures and (scored := score_track(fixtures[sid], result)) is not None
        ]
        fixed = aggregate([r for r in rows if not r["is_dynamic"]])
        dynamic = aggregate([r for r in rows if r["is_dynamic"]])

        serial = serial_by_name.get(full["candidate"], {})
        rtf = serial.get("realtime_factor")
        # A full-corpus pass is estimated on whole tracks, not 45s excerpts, so
        # scale by the surveyed median duration rather than the excerpt length.
        est_audio_s = args.corpus_tracks * 240.0
        estimate = f"{est_audio_s / rtf / 3600:.1f}h serial" if rtf else "n/a"

        candidates.append(
            {
                "label": full["candidate"],
                "version": full["candidate_version"],
                "license": full["license"],
                "shippable": full["shippable"],
                # None for a candidate that loads no weights (librosa, the constant
                # control). Present and non-null is what makes a model row's beats
                # attributable to a specific file rather than to a checkpoint NAME,
                # which is re-resolvable and therefore not provenance.
                "model": full.get("model"),
                "emits_downbeats": full["emits_downbeats"],
                "fixed": fixed,
                "dynamic": dynamic,
                "weighted": weight_cells(
                    fixed,
                    dynamic,
                    manifest["population"]["eligible_fixed"],
                    manifest["population"]["eligible_dynamic"],
                ),
                "runtime": {
                    "serial_p50_s": (serial.get("per_track_runtime_s") or {}).get("p50"),
                    "serial_realtime_factor": rtf,
                    "parallel_wall_s": full.get("wall_s"),
                    "parallel_workers": full.get("workers"),
                    "model_load_s": full.get("model_load_s"),
                    "full_corpus_estimate": estimate,
                },
                "n_failed": full.get("n_failed", 0),
                "tracks": rows,
            }
        )

    eval_partition = eval_partition_for_fixtures(fixtures)

    payload = {
        "schema": 1,
        "round": args.round,
        "scorer_version": SCORER_VERSION,
        "beat_tolerance_s": BEAT_TOLERANCE_S,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "eval_partition": eval_partition,
        "manifest": {
            "denominator": manifest["denominator"],
            "population": manifest["population"],
            "selection": manifest["selection"],
            "excerpt": manifest["excerpt"],
            "seed": manifest["seed"],
        },
        "candidates": candidates,
    }

    with open(args.out_json, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1)
    with open(args.out_md, "w", encoding="utf-8") as fh:
        fh.write(render_report(payload) + "\n")

    print(f"[report] scorer v{SCORER_VERSION} -> {args.out_md}")
    print(
        f"[report] eval partition ({eval_partition['predicate']}): "
        f"{eval_partition['n_fixed']} fixed, {eval_partition['n_dynamic']} dynamic"
    )
    for cand in candidates:
        fixed, dyn = cand["fixed"], cand["dynamic"]
        print(
            f"[report]   {cand['label']:24s} "
            f"fixed n={fixed.get('n', 0)} "
            f"F={fixed.get('f_measure_mean') or 0:.3f} "
            f"CMLt={fixed.get('cmlt_mean') or 0:.3f} AMLt={fixed.get('amlt_mean') or 0:.3f} "
            f"BPM<=1.0 {fixed.get('bpm_within_1_0_pct') or 0:.1f}% | "
            f"dyn n={dyn.get('n', 0)} "
            f"F={dyn.get('f_measure_mean') or 0:.3f} "
            f"CMLt={dyn.get('cmlt_mean') or 0:.3f} AMLt={dyn.get('amlt_mean') or 0:.3f}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
