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
import itertools
import json
import statistics
import sys
import time
from collections.abc import Sequence
from typing import Any

from scripts.beatbench.scorer import (
    BEAT_TOLERANCE_S,
    SCORER_VERSION,
    percentile,
    score_bpm,
    score_downbeats,
    score_positions,
    window_slice,
)

# ----- Per-track scoring --------------------------------------------------


def _derive_bpm(times: Sequence[float]) -> float | None:
    """Tempo from beat spacing: the one definition applied to every candidate.

    The median interval rather than the mean, so a single dropped or doubled
    beat cannot drag the estimate.
    """
    if len(times) < 3:
        return None
    gaps = [b - a for a, b in itertools.pairwise(times) if b > a]
    if not gaps:
        return None
    return 60.0 / statistics.median(gaps)


def score_track(fixture: dict[str, Any], result: dict[str, Any]) -> dict[str, Any] | None:
    """Score one candidate on one fixture, or None when it produced nothing."""
    start, end = fixture["score_start_s"], fixture["score_end_s"]
    offset = fixture["window_start_s"]

    ref_beats = [b[1] for b in fixture["ref_beats"]]
    ref_downbeats = [b[1] for b in fixture["ref_beats"] if b[0] == 1]
    ref_in = window_slice(ref_beats, start, end)
    ref_db_in = window_slice(ref_downbeats, start, end)
    if len(ref_in) < 8:
        return None

    if result.get("error"):
        cand_in: list[float] = []
        cand_db_in: list[float] | None = None
    else:
        cand_in = window_slice([t + offset for t in result["beats"]], start, end)
        cand_db_in = (
            None
            if result["downbeats"] is None
            else window_slice([t + offset for t in result["downbeats"]], start, end)
        )

    rb_stored = float(fixture["rb_bpm"])
    rb_window = _derive_bpm(ref_in)
    cand_bpm = _derive_bpm(cand_in)

    bpm_stored = score_bpm(rb_stored, cand_bpm)
    bpm_window = score_bpm(rb_window, cand_bpm) if rb_window else None
    positions = score_positions(ref_in, cand_in)
    downbeats = score_downbeats(ref_db_in, cand_db_in)

    return {
        "stable_id": fixture["stable_id"],
        "is_dynamic": fixture["is_dynamic"],
        "rb_bpm_stored": round(rb_stored, 2),
        "rb_bpm_window": round(rb_window, 3) if rb_window else None,
        "cand_bpm_derived": round(cand_bpm, 3) if cand_bpm else None,
        "cand_bpm_native": result.get("native_bpm"),
        "bpm_vs_stored": {
            "abs_error": bpm_stored.abs_error,
            "within_0_01": bpm_stored.within_0_01,
            "within_0_1": bpm_stored.within_0_1,
            "within_1_0": bpm_stored.within_1_0,
            "relation": bpm_stored.relation,
        },
        "bpm_vs_window": None
        if bpm_window is None
        else {
            "abs_error": bpm_window.abs_error,
            "within_0_01": bpm_window.within_0_01,
            "within_0_1": bpm_window.within_0_1,
            "within_1_0": bpm_window.within_1_0,
            "relation": bpm_window.relation,
        },
        "positions": {
            "n_reference": positions.n_reference,
            "n_candidate": positions.n_candidate,
            "beat_count_ratio": round(positions.beat_count_ratio, 4),
            "f_measure": round(positions.f_measure, 4),
            "raw_p50_ms": None if positions.raw_p50_ms is None else round(positions.raw_p50_ms, 2),
            "raw_p95_ms": None if positions.raw_p95_ms is None else round(positions.raw_p95_ms, 2),
            "global_shift_ms": None
            if positions.global_shift_ms is None
            else round(positions.global_shift_ms, 2),
            "shifted_p50_ms": None
            if positions.shifted_p50_ms is None
            else round(positions.shifted_p50_ms, 2),
            "shifted_p95_ms": None
            if positions.shifted_p95_ms is None
            else round(positions.shifted_p95_ms, 2),
        },
        "downbeats": {
            "supported": downbeats.supported,
            "agreement": None if downbeats.agreement is None else round(downbeats.agreement, 4),
        },
        "error": result.get("error"),
    }


# ----- Aggregation --------------------------------------------------------


def _pct(rows: list[dict], path: tuple[str, ...]) -> float | None:
    if not rows:
        return None
    hits = 0
    for row in rows:
        node: Any = row
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
            if node is None:
                break
        hits += bool(node)
    return 100.0 * hits / len(rows)


def _median_of(rows: list[dict], section: str, field: str) -> float | None:
    values = [r[section][field] for r in rows if r[section].get(field) is not None]
    return percentile(values, 50)


def aggregate(rows: list[dict]) -> dict[str, Any]:
    """Collapse per-track scores into one cell of the table."""
    if not rows:
        return {"n": 0}
    relations = [r["bpm_vs_stored"]["relation"] for r in rows]
    db_rows = [r for r in rows if r["downbeats"]["supported"]]
    return {
        "n": len(rows),
        "bpm_exact_0_01_pct": _pct(rows, ("bpm_vs_stored", "within_0_01")),
        "bpm_within_0_1_pct": _pct(rows, ("bpm_vs_stored", "within_0_1")),
        "bpm_within_1_0_pct": _pct(rows, ("bpm_vs_stored", "within_1_0")),
        "bpm_window_within_1_0_pct": _pct(rows, ("bpm_vs_window", "within_1_0")),
        "octave_half_pct": 100.0 * relations.count("half") / len(rows),
        "octave_double_pct": 100.0 * relations.count("double") / len(rows),
        "octave_other_pct": 100.0
        * sum(1 for r in relations if r in ("two_thirds", "three_halves", "third", "triple"))
        / len(rows),
        "unrelated_pct": 100.0 * relations.count("none") / len(rows),
        "f_measure_mean": statistics.fmean(r["positions"]["f_measure"] for r in rows),
        "raw_p50_ms": _median_of(rows, "positions", "raw_p50_ms"),
        "raw_p95_ms": _median_of(rows, "positions", "raw_p95_ms"),
        "shifted_p50_ms": _median_of(rows, "positions", "shifted_p50_ms"),
        "shifted_p95_ms": _median_of(rows, "positions", "shifted_p95_ms"),
        "abs_global_shift_p50_ms": percentile(
            [abs(r["positions"]["global_shift_ms"]) for r in rows
             if r["positions"]["global_shift_ms"] is not None], 50
        ),
        "downbeat_supported": bool(db_rows),
        "downbeat_agreement_mean": (
            100.0 * statistics.fmean(r["downbeats"]["agreement"] for r in db_rows)
            if db_rows else None
        ),
        "n_errored": sum(1 for r in rows if r["error"]),
    }


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


# ----- Rendering ----------------------------------------------------------


def _f(value: Any, spec: str = ".1f") -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return format(value, spec)


def render_table(title: str, cells: list[tuple[str, dict, dict]]) -> list[str]:
    """One markdown table: a row per candidate for a single cell of the split."""
    head = (
        "| candidate | n | BPM exact | BPM <=0.1 | BPM <=1.0 | half | double | "
        "F@70ms | raw p50 | raw p95 | shift p50 | shift p95 | downbeat |"
    )
    rule = "|" + "|".join(["---"] * 13) + "|"
    lines = [f"### {title}", "", head, rule]
    for label, cell, meta in cells:
        if not cell.get("n"):
            lines.append(f"| {label} | 0 |" + " n/a |" * 11)
            continue
        db = "N/A" if not meta.get("emits_downbeats") else _f(cell["downbeat_agreement_mean"]) + "%"
        lines.append(
            f"| {label} | {cell['n']} | {_f(cell['bpm_exact_0_01_pct'])}% | "
            f"{_f(cell['bpm_within_0_1_pct'])}% | {_f(cell['bpm_within_1_0_pct'])}% | "
            f"{_f(cell['octave_half_pct'])}% | {_f(cell['octave_double_pct'])}% | "
            f"{_f(cell['f_measure_mean'], '.3f')} | {_f(cell['raw_p50_ms'])} | "
            f"{_f(cell['raw_p95_ms'])} | {_f(cell['shifted_p50_ms'])} | "
            f"{_f(cell['shifted_p95_ms'])} | {db} |"
        )
    lines.append("")
    return lines


def render_report(payload: dict[str, Any]) -> str:
    man = payload["manifest"]
    den = man["denominator"]
    pop = man["population"]
    lines = [
        "# Beat-mapping benchmark, round 0",
        "",
        f"Generated {payload['generated_at']} with scorer v{payload['scorer_version']}, "
        f"beat tolerance {int(BEAT_TOLERANCE_S * 1000)} ms.",
        "",
        "Ground truth is rekordbox's own ANLZ PQTZ beat grid, read through the lane daemon. "
        "The KPI is agreement with rekordbox, so every figure below is a distance from what "
        "rekordbox already believes, not an independent judgement of correctness.",
        "",
        "> **KNOWN ARTIFACT, scorer v1.0.0: do not quote the BPM columns.** Candidate "
        "tempo is derived as `60 / median(inter-beat interval)`. Quantized beat times "
        "snap that median, so the derived BPM lands a clean 1.0 or 2.0 BPM off. "
        "Re-deriving from the SAME beat times by least-squares fit moves beat_this from "
        "47.0% to 82.5% within 1.0 BPM on fixed grids. Every BPM column below "
        "understates every candidate. The position columns (F-measure, raw and shifted "
        "offsets, downbeat agreement) come straight from raw beat times and are "
        "unaffected. Fix is resume item 1 in specs/beat-mapping-bench.md.",
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
        "## Round-0 table",
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
        "are the same after removing the best global shift, so raw-minus-shifted is constant "
        "phase error and shifted alone is jitter. downbeat is agreement with rekordbox bar-1 "
        "beats, N/A where the analyzer has no downbeat concept.",
        "",
        "## Runtime",
        "",
        "| candidate | serial per-track p50 | serial realtime factor | parallel wall | "
        "workers | est. full corpus |",
        "|---|---|---|---|---|---|",
    ]
    for cand in payload["candidates"]:
        rt = cand["runtime"]
        lines.append(
            f"| {cand['label']} | {_f(rt.get('serial_p50_s'), '.2f')}s | "
            f"{_f(rt.get('serial_realtime_factor'), '.1f')}x | "
            f"{_f(rt.get('parallel_wall_s'), '.0f')}s | {rt.get('parallel_workers', 'n/a')} | "
            f"{rt.get('full_corpus_estimate', 'n/a')} |"
        )
    lines += [
        "",
        "Serial figures are measured one candidate at a time with a single worker and nothing "
        "else running: that is the honest per-track cost. Parallel wall is the same full pass "
        "with several workers, useful only for planning how long a batch takes. An earlier "
        "measurement that ran all five candidates concurrently understated librosa by roughly "
        "85x through CPU contention alone, which is why the two are reported separately.",
        "",
        "## Licensing, which constrains the answer as much as accuracy does",
        "",
        "| candidate | licence | shippable |",
        "|---|---|---|",
    ]
    for cand in payload["candidates"]:
        lines.append(
            f"| {cand['label']} | {cand['license']} | {'yes' if cand['shippable'] else 'NO'} |"
        )
    lines.append("")
    return "\n".join(lines)


# ----- Entry point --------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fixtures", required=True)
    ap.add_argument("--full", nargs="+", required=True, help="full-pass candidate JSONs")
    ap.add_argument("--serial", nargs="*", default=[], help="serial calibration JSONs")
    ap.add_argument("--out-md", required=True)
    ap.add_argument("--out-json", required=True)
    ap.add_argument("--corpus-tracks", type=int, default=10000)
    args = ap.parse_args()

    with open(args.fixtures, encoding="utf-8") as fh:
        manifest = json.load(fh)
    fixtures = {f["stable_id"]: f for f in manifest["fixtures"]}

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

        candidates.append({
            "label": full["candidate"],
            "version": full["candidate_version"],
            "license": full["license"],
            "shippable": full["shippable"],
            "emits_downbeats": full["emits_downbeats"],
            "fixed": fixed,
            "dynamic": dynamic,
            "weighted": weight_cells(
                fixed, dynamic,
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
        })

    payload = {
        "schema": 1,
        "round": 0,
        "scorer_version": SCORER_VERSION,
        "beat_tolerance_s": BEAT_TOLERANCE_S,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
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
    for cand in candidates:
        fixed, dyn = cand["fixed"], cand["dynamic"]
        print(
            f"[report]   {cand['label']:34s} fixed F={fixed.get('f_measure_mean', 0):.3f} "
            f"dyn F={dyn.get('f_measure_mean', 0):.3f} "
            f"fixed BPM<=1.0 {fixed.get('bpm_within_1_0_pct', 0):.0f}%"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
