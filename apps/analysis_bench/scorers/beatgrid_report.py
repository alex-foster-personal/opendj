"""Beatgrid per-track scoring, aggregation, and the per-cell markdown table.

Supersedes: the scoring functions previously defined in
``scripts/beatbench/report.py``. That module re-exports this one so the
round-report CLI (``python -m scripts.beatbench.report``) and
``tests/beatbench`` keep the import path they already pin. The functions
live under ``apps`` so a lane scorer does not import ``scripts``.
"""

from __future__ import annotations

import json
import os
import statistics
from collections.abc import Sequence
from typing import Any

from apps.analysis_bench.scorers.beatgrid import (
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


def _join_truth(manifest_path: str, manifest: dict[str, Any], missing: list[str]) -> dict[str, list]:
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
    measurement that replaced it and (private path) round 0 for
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
            None if result["downbeats"] is None else window_slice([t + offset for t in result["downbeats"]], start, end)
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
            "global_shift_ms": None if positions.global_shift_ms is None else round(positions.global_shift_ms, 2),
            "shifted_p50_ms": None if positions.shifted_p50_ms is None else round(positions.shifted_p50_ms, 2),
            "shifted_p95_ms": None if positions.shifted_p95_ms is None else round(positions.shifted_p95_ms, 2),
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
        "f_measure_shifted_mean": statistics.fmean(r["positions"]["f_measure_shifted"] for r in rows),
        "cmlt_mean": _mean_optional(rows, "continuity", "cmlt"),
        "amlt_mean": _mean_optional(rows, "continuity", "amlt"),
        "cmlc_mean": _mean_optional(rows, "continuity", "cmlc"),
        "amlc_mean": _mean_optional(rows, "continuity", "amlc"),
        "n_continuity_scored": sum(1 for r in rows if r["continuity"]["cmlt"] is not None),
        "n_emitted_nothing": sum(1 for r in rows if r["positions"]["n_candidate"] == 0),
        "n_under_half_reference": sum(1 for r in rows if r["positions"]["beat_count_ratio"] < 0.5),
        "beat_count_ratio_p50": _median_of(rows, "positions", "beat_count_ratio"),
        "raw_p50_ms": _median_of(rows, "positions", "raw_p50_ms"),
        "raw_p95_ms": _median_of(rows, "positions", "raw_p95_ms"),
        "shifted_p50_ms": _median_of(rows, "positions", "shifted_p50_ms"),
        "shifted_p95_ms": _median_of(rows, "positions", "shifted_p95_ms"),
        "abs_global_shift_p50_ms": percentile(
            [abs(r["positions"]["global_shift_ms"]) for r in rows if r["positions"]["global_shift_ms"] is not None], 50
        ),
        "downbeat_supported": bool(db_rows),
        "downbeat_agreement_mean": (
            100.0 * statistics.fmean(r["downbeats"]["agreement"] for r in db_rows) if db_rows else None
        ),
        "n_errored": sum(1 for r in rows if r["error"]),
    }


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
