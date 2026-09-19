"""META-02: flag tracks likely to have bad beatgrids.

Three independent reasons (any one fires -> flag):

* ``DOWNBEAT_TRANSIENT_GAP``: first downbeat is more than
  ``max_downbeat_transient_gap_beats`` beats after the first transient.
* ``BPM_DRIFT``: stddev(bpm_per_frame) / mean(bpm_per_frame) is larger than
  ``max_bpm_drift_pct / 100`` AND no madmom tempo-change marker is present
  (we approximate that as any inter-frame delta > 5 %).
* ``CUE_OFF_GRID``: an existing RB / djay cue point lies more than
  ``cue_grid_tolerance_ms`` from the nearest analyser beat.

Inputs: :class:`AnalysisRecord` rows loaded from state; optional per-track
cue lists (seconds).  Outputs: a CSV report at
``data/analysis/beatgrid-flags.csv`` + a ``beatgrid.flag`` event per row
through :func:`apps.analysis.store.publish_event`.

This module is read-only on RB / djay DBs.
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.table import Table

from apps.shared.paths import DATA_DIR

from . import config as _analysis_config
from .backends import BACKENDS, DEFAULT_BACKEND  # noqa: F401  (ensures registry populated)
from .record import AnalysisRecord
from .store import fetch_records_by_ids, publish_event

log = logging.getLogger("apps.analysis.detect_bad_beatgrid")
console = Console()

DEFAULT_OUTPUT: Path = DATA_DIR / "analysis" / "beatgrid-flags.csv"

REASON_DOWNBEAT = "DOWNBEAT_TRANSIENT_GAP"
REASON_DRIFT = "BPM_DRIFT"
REASON_CUE_OFF_GRID = "CUE_OFF_GRID"


# ---------------------------------------------------------------------------
# Core checks
# ---------------------------------------------------------------------------


@dataclass
class Flag:
    stable_id: str
    reasons: list[str] = field(default_factory=list)
    first_transient_s: float | None = None
    first_downbeat_s: float | None = None
    bpm_drift_pct: float | None = None
    max_cue_offset_ms: float | None = None
    title: str = ""
    artist: str = ""


def _check_downbeat_gap(
    record: AnalysisRecord, *, max_beats: float
) -> tuple[bool, float | None, float | None]:
    if not record.downbeats_s or not record.onsets_s or record.bpm <= 0:
        return False, None, None
    first_transient = float(record.onsets_s[0])
    first_downbeat = float(record.downbeats_s[0])
    gap_s = first_downbeat - first_transient
    threshold_s = max_beats * 60.0 / record.bpm
    return gap_s > threshold_s, first_transient, first_downbeat


def _check_bpm_drift(
    record: AnalysisRecord, *, max_pct: float
) -> tuple[bool, float | None]:
    import numpy as np
    bpms = record.features_blob.get("bpm_per_frame", []) if record.features_blob else []
    if len(bpms) < 4:
        return False, None
    arr = np.asarray(bpms, dtype=float)
    mean = float(np.mean(arr))
    if mean <= 0:
        return False, None
    # Approximate tempo-change marker: any inter-frame delta > 5 % of mean.
    inter = np.abs(np.diff(arr))
    has_marker = bool(np.any(inter > 0.05 * mean))
    drift_pct = float(np.std(arr) / mean) * 100.0
    flagged = drift_pct > max_pct and not has_marker
    return flagged, drift_pct


def _check_cue_off_grid(
    record: AnalysisRecord,
    cues_s: list[float],
    *,
    tolerance_ms: float,
) -> tuple[bool, float | None]:
    """Return ``(flagged, max_offset_ms)`` for ``cues_s`` on this track."""
    if not cues_s or record.bpm <= 0:
        return False, None
    # Predicted grid from the first downbeat (or first beat) at the reported
    # BPM.  If no downbeats, fall back to the first onset.
    anchor = (
        record.downbeats_s[0] if record.downbeats_s
        else (record.onsets_s[0] if record.onsets_s else 0.0)
    )
    beat_period_s = 60.0 / record.bpm
    max_offset = 0.0
    flagged = False
    tol_s = tolerance_ms / 1000.0
    for cue in cues_s:
        # Nearest predicted beat: snap to (cue - anchor) modulo beat_period.
        delta = cue - anchor
        snap_offset = abs(delta - round(delta / beat_period_s) * beat_period_s)
        if snap_offset > max_offset:
            max_offset = snap_offset
        if snap_offset > tol_s:
            flagged = True
    return flagged, max_offset * 1000.0


def detect_one(
    record: AnalysisRecord,
    *,
    cues_s: list[float] | None = None,
    cfg: dict | None = None,
) -> Flag:
    cfg = cfg or _analysis_config.load_config()
    b = cfg["beatgrid_detector"]
    flag = Flag(stable_id=record.stable_id)

    ok_db, ft, fd = _check_downbeat_gap(
        record, max_beats=float(b["max_downbeat_transient_gap_beats"])
    )
    flag.first_transient_s = ft
    flag.first_downbeat_s = fd
    if ok_db:
        flag.reasons.append(REASON_DOWNBEAT)

    ok_dr, drift_pct = _check_bpm_drift(
        record, max_pct=float(b["max_bpm_drift_pct"])
    )
    flag.bpm_drift_pct = drift_pct
    if ok_dr:
        flag.reasons.append(REASON_DRIFT)

    ok_cue, max_off = _check_cue_off_grid(
        record,
        list(cues_s or []),
        tolerance_ms=float(b["cue_grid_tolerance_ms"]),
    )
    flag.max_cue_offset_ms = max_off
    if ok_cue:
        flag.reasons.append(REASON_CUE_OFF_GRID)

    return flag


# ---------------------------------------------------------------------------
# Report writer
# ---------------------------------------------------------------------------


CSV_COLUMNS: tuple[str, ...] = (
    "stable_id",
    "title",
    "artist",
    "reasons",
    "first_transient_s",
    "first_downbeat_s",
    "bpm_drift_pct",
    "max_cue_offset_ms",
)


def write_csv(flags: list[Flag], output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_COLUMNS)
        for f in flags:
            w.writerow([
                f.stable_id,
                f.title,
                f.artist,
                "|".join(f.reasons),
                "" if f.first_transient_s is None else f"{f.first_transient_s:.4f}",
                "" if f.first_downbeat_s is None else f"{f.first_downbeat_s:.4f}",
                "" if f.bpm_drift_pct is None else f"{f.bpm_drift_pct:.4f}",
                "" if f.max_cue_offset_ms is None else f"{f.max_cue_offset_ms:.2f}",
            ])
    return output


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def run(
    *,
    records: Iterable[AnalysisRecord],
    cues_by_stable_id: dict[str, list[float]] | None = None,
    limit: int | None = None,
    reasons_include: set[str] | None = None,
    reasons_exclude: set[str] | None = None,
    output: Path = DEFAULT_OUTPUT,
    db_path: Path | None = None,
    emit_events: bool = True,
) -> list[Flag]:
    """Run the detector over ``records`` and write a CSV report."""
    cues_by_stable_id = cues_by_stable_id or {}
    flags: list[Flag] = []
    for i, rec in enumerate(records):
        if limit is not None and i >= limit:
            break
        f = detect_one(rec, cues_s=cues_by_stable_id.get(rec.stable_id))
        if not f.reasons:
            continue
        if reasons_include and not (set(f.reasons) & reasons_include):
            continue
        if reasons_exclude and (set(f.reasons) & reasons_exclude):
            continue
        flags.append(f)
        if emit_events:
            publish_event(
                "beatgrid.flag",
                {
                    "stable_id": rec.stable_id,
                    "reasons": list(f.reasons),
                    "first_transient_s": f.first_transient_s,
                    "first_downbeat_s": f.first_downbeat_s,
                    "bpm_drift_pct": f.bpm_drift_pct,
                    "max_cue_offset_ms": f.max_cue_offset_ms,
                },
                stable_id=rec.stable_id,
                db_path=db_path,
            )

    write_csv(flags, output)
    return flags


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m apps.analysis.detect_bad_beatgrid",
        description="Flag tracks with likely bad beatgrids (META-02).",
    )
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--stable-ids", nargs="+", default=None)
    p.add_argument("--reasons-include", nargs="+", default=None)
    p.add_argument("--reasons-exclude", nargs="+", default=None)
    p.add_argument("--backend", default=DEFAULT_BACKEND)
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    p.add_argument("--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.verbose:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.stable_ids:
        records = fetch_records_by_ids(args.stable_ids, backend=args.backend)
    else:
        # No ids given -> dump every row for this backend.
        from apps.analysis import store as _store
        rows = _store.fetch_records(backend=args.backend)
        records = [AnalysisRecord.from_json(r["record_json"]) for r in rows]

    flags = run(
        records=records,
        limit=args.limit,
        reasons_include=set(args.reasons_include) if args.reasons_include else None,
        reasons_exclude=set(args.reasons_exclude) if args.reasons_exclude else None,
        output=args.output,
    )

    table = Table(title=f"beatgrid flags ({len(flags)})")
    table.add_column("stable_id")
    table.add_column("reasons")
    table.add_column("first_downbeat_s")
    table.add_column("bpm_drift_%")
    table.add_column("max_cue_off_ms")
    for f in flags[:20]:
        table.add_row(
            f.stable_id[:18],
            "|".join(f.reasons),
            "-" if f.first_downbeat_s is None else f"{f.first_downbeat_s:.2f}",
            "-" if f.bpm_drift_pct is None else f"{f.bpm_drift_pct:.2f}",
            "-" if f.max_cue_offset_ms is None else f"{f.max_cue_offset_ms:.1f}",
        )
    console.print(table)
    console.print(f"[green]ok[/green] wrote {args.output}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
