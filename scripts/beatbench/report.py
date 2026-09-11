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
import os
import statistics
import sys
import time
from collections.abc import Sequence
from typing import Any

from apps.analysis_bench.scorers.beatgrid import (
    BEAT_TOLERANCE_S,
    SCORER_VERSION,
    WEIGHTS_NOT_RELEASED,
    grid_is_dynamic,
    least_squares_bpm,
    partition_counts,
    percentile,
    reserved_table_cells,
    score_bpm,
    score_continuity,
    score_downbeats,
    score_positions,
    window_slice,
)

# ----- Loading the question -----------------------------------------------


def _join_truth(
    manifest_path: str, manifest: dict[str, Any], missing: list[str]
) -> dict[str, list]:
    """Beats for `missing`, read from the truth file the manifest declares.

    Raises rather than skipping: an unjoined fixture reaches `score_track` as an
    empty grid, is dropped by its `len(ref_in) < 8` guard, and a half-joined
    bundle then produces a table with a quietly smaller denominator.
    """
    truth_name = (manifest.get("reads") or {}).get("truth")
    if not truth_name:
        raise SystemExit(
            f"[report] {len(missing)} fixtures in {manifest_path} carry no `ref_beats` "
            "and the manifest declares no `reads.truth` file to join them from"
        )
    truth_path = os.path.join(os.path.dirname(os.path.abspath(manifest_path)), truth_name)
    if not os.path.exists(truth_path):
        raise SystemExit(f"[report] {manifest_path} points at {truth_path}, which is not there")

    with open(truth_path, encoding="utf-8") as fh:
        beats = json.load(fh)["beats"]
    absent = [sid for sid in missing if sid not in beats]
    if absent:
        raise SystemExit(
            f"[report] {truth_path} holds no beats for {len(absent)} of the "
            f"{len(missing)} fixtures that need them, first {absent[0]}"
        )
    print(f"[report] joined {len(missing)} fixtures with truth from {truth_path}", flush=True)
    return beats


def load_fixtures(manifest_path: str) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """`(manifest, fixtures by stable id)` with the reference beats attached.

    A BENCH manifest carries `ref_beats` inline. A portable BUNDLE does not:
    `bundle.py` moves the grids into `rekordbox-truth.json` so a consuming host
    can checksum the truth apart from the audio, which left the scorer indexing
    a key that was not there. The join happens here so both shapes score alike.
    """
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    fixtures = {f["stable_id"]: f for f in manifest["fixtures"]}

    missing = [sid for sid, fixture in fixtures.items() if "ref_beats" not in fixture]
    if missing:
        beats = _join_truth(manifest_path, manifest, missing)
        for sid in missing:
            fixtures[sid] = {**fixtures[sid], "ref_beats": beats[sid]}
    return manifest, fixtures


# ----- Per-track scoring --------------------------------------------------


def _derive_bpm(times: Sequence[float]) -> float | None:
    """Tempo for one grid, from the scorer, so the ruler lives in exactly one file.

    Was `60 / median(inter-beat interval)` through scorer v1.0.0. That
    estimator inherited each analyzer's frame quantization and understated
    EVERY candidate's BPM columns; see `scorer.least_squares_bpm` for the
    measurement that replaced it and specs/beat-mapping-bench.md round 0 for
    the 47.0 vs 82.5 percent figure it moved.
    """
    return least_squares_bpm(times)


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
    continuity = score_continuity(ref_in, cand_in)
    downbeats = score_downbeats(ref_db_in, cand_db_in)

    return {
        "stable_id": fixture["stable_id"],
        "is_dynamic": grid_is_dynamic(fixture["ref_beats"]),
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
            "f_measure_shifted": round(positions.f_measure_shifted, 4),
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
        "continuity": {
            "cmlc": None if continuity.cmlc is None else round(continuity.cmlc, 4),
            "cmlt": None if continuity.cmlt is None else round(continuity.cmlt, 4),
            "amlc": None if continuity.amlc is None else round(continuity.amlc, 4),
            "amlt": None if continuity.amlt is None else round(continuity.amlt, 4),
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


def _mean_optional(rows: list[dict], section: str, field: str) -> float | None:
    """Mean over the rows that HAVE the value, never over a substituted zero.

    A candidate too sparse to score continuity on must shrink the denominator,
    not post a zero: `n_continuity_scored` beside it is what names that
    denominator so a reader can see how many rows the mean is over.
    """
    values = [r[section][field] for r in rows if r[section].get(field) is not None]
    return statistics.fmean(values) if values else None


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
        "f_measure_shifted_mean": statistics.fmean(
            r["positions"]["f_measure_shifted"] for r in rows
        ),
        "cmlt_mean": _mean_optional(rows, "continuity", "cmlt"),
        "amlt_mean": _mean_optional(rows, "continuity", "amlt"),
        "cmlc_mean": _mean_optional(rows, "continuity", "cmlc"),
        "amlc_mean": _mean_optional(rows, "continuity", "amlc"),
        "n_continuity_scored": sum(
            1 for r in rows if r["continuity"]["cmlt"] is not None
        ),
        "n_emitted_nothing": sum(1 for r in rows if r["positions"]["n_candidate"] == 0),
        "n_under_half_reference": sum(
            1 for r in rows if r["positions"]["beat_count_ratio"] < 0.5
        ),
        "beat_count_ratio_p50": _median_of(rows, "positions", "beat_count_ratio"),
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


def _unit(value: Any, spec: str, unit: str) -> str:
    """A value with its unit, or a bare `n/a`: `n/as` and `n/ax` read like values."""
    return "n/a" if value is None else f"{format(value, spec)}{unit}"


def eval_partition_for_fixtures(fixtures: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Re-count fixed vs dynamic for fixtures that survive the scoring window guard."""
    ref_beats_by_track: list[list] = []
    for fixture in fixtures.values():
        start, end = fixture["score_start_s"], fixture["score_end_s"]
        ref_beats = [b[1] for b in fixture["ref_beats"]]
        if len(window_slice(ref_beats, start, end)) >= 8:
            ref_beats_by_track.append(fixture["ref_beats"])
    n_fixed, n_dynamic = partition_counts(ref_beats_by_track)
    return {
        "n_fixed": n_fixed,
        "n_dynamic": n_dynamic,
        "predicate": "grid_is_dynamic",
    }


def render_table(title: str, cells: list[tuple[str, dict, dict]]) -> list[str]:
    """One markdown table: a row per candidate for a single cell of the split."""
    head = (
        "| candidate | n | BPM exact | BPM <=0.1 | BPM <=1.0 | half | double | "
        "F@70ms | F shifted | CMLt | AMLt | cont. n | raw p50 | raw p95 | "
        "shift p50 | shift p95 | downbeat |"
    )
    rule = "|" + "|".join(["---"] * 17) + "|"
    lines = [f"### {title}", "", head, rule]
    for label, cell, meta in cells + reserved_table_cells():
        status = cell.get("status") or meta.get("status")
        if status == WEIGHTS_NOT_RELEASED:
            lines.append(f"| {label} | {WEIGHTS_NOT_RELEASED} |" + " n/a |" * 15)
            continue
        if not cell.get("n"):
            lines.append(f"| {label} | 0 |" + " n/a |" * 15)
            continue
        db = "N/A" if not meta.get("emits_downbeats") else _f(cell["downbeat_agreement_mean"]) + "%"
        lines.append(
            f"| {label} | {cell['n']} | {_f(cell['bpm_exact_0_01_pct'])}% | "
            f"{_f(cell['bpm_within_0_1_pct'])}% | {_f(cell['bpm_within_1_0_pct'])}% | "
            f"{_f(cell['octave_half_pct'])}% | {_f(cell['octave_double_pct'])}% | "
            f"{_f(cell['f_measure_mean'], '.3f')} | "
            f"{_f(cell['f_measure_shifted_mean'], '.3f')} | "
            f"{_f(cell['cmlt_mean'], '.3f')} | {_f(cell['amlt_mean'], '.3f')} | "
            f"{cell['n_continuity_scored']} | {_f(cell['raw_p50_ms'])} | "
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
        "specs/beat-mapping-bench.md.",
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
        "| candidate | serial per-track p50 | serial realtime factor | parallel wall | "
        "workers | est. full corpus |",
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
        "entry in specs/beat-mapping-bench.md is authoritative on the load actually carried. "
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
        lines.append(
            f"| {cand['label']} | {cand['license']} | {'yes' if cand['shippable'] else 'NO'} |"
        )
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

        candidates.append({
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
