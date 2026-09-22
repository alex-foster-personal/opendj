"""META-04: propose up to 8 labelled hot cues per track.

Heuristic (pure-function + deterministic):

1. Segment the track into ``bin_count`` equal-time bins.
2. Within each bin, pick the onset with the highest local RMS as the
   candidate cue time.
3. Snap each candidate to the nearest downbeat within
   ``downbeat_snap_tolerance_ms``; otherwise keep the onset unchanged.
4. Deduplicate: sort by time, drop any cue within ``min_cue_gap_ms`` of a
   higher-RMS neighbour.
5. Label:
   * bin 0 cue -> ``intro``
   * cue at the global RMS max -> ``drop``
   * cue in the bin immediately after ``drop`` with a local mean RMS below
     0.6x the track median -> ``break``
   * last-bin cue -> ``outro``
   * rest -> unlabelled (empty string)
6. Cap at ``max_cues_per_track``.

Output: ``data/analysis/cue-proposals.json`` + a ``cue.proposal`` event
per track.  Read-only on audio files and RB / djay.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from rich.console import Console
from rich.table import Table

from apps.shared.paths import DATA_DIR

from . import config as _analysis_config
from .backends import DEFAULT_BACKEND
from .record import AnalysisRecord
from .store import fetch_records_by_ids, publish_event

log = logging.getLogger("apps.analysis.auto_cues")
console = Console()

DEFAULT_OUTPUT: Path = DATA_DIR / "analysis" / "cue-proposals.json"


@dataclass
class CueProposal:
    time_s: float
    label: str  # "intro" | "drop" | "break" | "outro" | ""
    confidence: float
    rms_dbfs: float


@dataclass
class TrackProposal:
    stable_id: str
    backend_version: str
    cues: list[CueProposal] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------


def _rms_at_times(
    times_s: list[float],
    rms: list[float],
    rms_hop: int,  # noqa: ARG001
    sr: int = 44100,  # noqa: ARG001
) -> list[float]:
    """Sample the (downsampled) RMS envelope at ``times_s``."""
    if not rms or not times_s:
        return [0.0] * len(times_s)
    arr = np.asarray(rms, dtype=float)
    # The stored RMS is downsampled to <=512 frames over the whole duration.
    # We know len(rms) and we can scale by the ratio of index:duration.
    # We do not carry the full-frame count but the caller passes rms_hop for
    # the original hop; we fall back to a uniform mapping.
    n = arr.size
    total_time = max(times_s) * 1.05 + 1e-3
    out: list[float] = []
    for t in times_s:
        idx = round((t / total_time) * (n - 1))
        idx = max(0, min(n - 1, idx))
        out.append(float(arr[idx]))
    return out


def _pick_bin_candidates(
    onsets_s: list[float],
    rms_at_onsets: list[float],
    *,
    duration_s: float,
    bin_count: int,
) -> list[tuple[float, float]]:
    """Return ``[(time_s, rms_value), ...]`` one per non-empty bin."""
    if duration_s <= 0 or bin_count <= 0:
        return []
    if not onsets_s:
        return []
    bin_width = duration_s / bin_count
    best_per_bin: dict[int, tuple[float, float]] = {}
    # strict: rms_at_onsets is computed per onset; drift is a bug upstream.
    for t, r in zip(onsets_s, rms_at_onsets, strict=True):
        b = min(bin_count - 1, int(t / bin_width))
        prev = best_per_bin.get(b)
        if prev is None or r > prev[1]:
            best_per_bin[b] = (t, r)
    return [best_per_bin[b] for b in sorted(best_per_bin)]


def _snap_to_downbeat(
    time_s: float, downbeats_s: list[float], tol_ms: float
) -> float:
    if not downbeats_s:
        return time_s
    tol = tol_ms / 1000.0
    nearest = min(downbeats_s, key=lambda d: abs(d - time_s))
    if abs(nearest - time_s) <= tol:
        return nearest
    return time_s


def _deduplicate(
    cues: list[tuple[float, float]], *, min_gap_ms: float
) -> list[tuple[float, float]]:
    if not cues:
        return []
    gap_s = min_gap_ms / 1000.0
    cues = sorted(cues, key=lambda c: c[0])
    out: list[tuple[float, float]] = []
    for c in cues:
        if out and (c[0] - out[-1][0]) < gap_s:
            # Keep the louder of the two.
            if c[1] > out[-1][1]:
                out[-1] = c
            continue
        out.append(c)
    return out


def _label_cues(
    cues: list[tuple[float, float]],
    *,
    duration_s: float,
    rms_dbfs_values: list[float],
    bin_count: int,
) -> list[CueProposal]:
    if not cues:
        return []
    bin_width = duration_s / bin_count
    # Global maximum position for "drop" label.
    drop_idx = int(np.argmax([r for _, r in cues]))
    # Track median RMS (of cues) for break threshold.
    if rms_dbfs_values:
        median_rms = float(np.median(rms_dbfs_values))
    else:
        median_rms = float(np.median([r for _, r in cues]))
    last_bin = bin_count - 1
    # P06-F03: the "break" cue should be the cue whose time bin is adjacent
    # to (one bin after) the drop's bin, not merely the next cue in the
    # list. Otherwise a cue that happens to follow the drop in list order
    # but sits several bins away gets mislabelled.
    drop_bin = min(last_bin, int(cues[drop_idx][0] / bin_width))
    out: list[CueProposal] = []
    for i, (t, r) in enumerate(cues):
        b = min(last_bin, int(t / bin_width))
        label = ""
        if i == 0 and b == 0:
            label = "intro"
        elif i == drop_idx:
            label = "drop"
        elif b == last_bin and i == len(cues) - 1:
            label = "outro"
        # "break" candidate: bin immediately after drop's bin with
        # RMS < 0.6 * median. Skip the drop cue itself.
        if (
            label == ""
            and i != drop_idx
            and b == drop_bin + 1
            and r < 0.6 * median_rms
        ):
            label = "break"
        rms_dbfs = float(20.0 * np.log10(max(r, 1e-9)))
        # Confidence: relative RMS among cues, clipped [0, 1].
        max_r = max((rr for _, rr in cues), default=r)
        conf = float(min(1.0, r / (max_r + 1e-9)))
        out.append(CueProposal(
            time_s=float(t), label=label, confidence=conf, rms_dbfs=rms_dbfs,
        ))
    return out


def propose_cues(
    record: AnalysisRecord, *, cfg: dict | None = None
) -> TrackProposal:
    cfg = cfg or _analysis_config.load_config()
    ac = cfg["auto_cues"]
    bin_count = int(ac["bin_count"])
    min_gap = float(ac["min_cue_gap_ms"])
    snap_tol = float(ac["downbeat_snap_tolerance_ms"])
    max_cues = int(ac["max_cues_per_track"])

    rms = record.features_blob.get("rms", []) if record.features_blob else []
    rms_hop = int(record.features_blob.get("rms_hop", 512)) if record.features_blob else 512
    rms_at_onsets = _rms_at_times(
        record.onsets_s, rms, rms_hop=rms_hop, sr=record.sample_rate
    )

    candidates = _pick_bin_candidates(
        record.onsets_s, rms_at_onsets,
        duration_s=record.duration_s, bin_count=bin_count,
    )

    snapped = [
        (_snap_to_downbeat(t, record.downbeats_s, snap_tol), r)
        for t, r in candidates
    ]
    deduped = _deduplicate(snapped, min_gap_ms=min_gap)
    labelled = _label_cues(
        deduped,
        duration_s=record.duration_s,
        rms_dbfs_values=rms_at_onsets,
        bin_count=bin_count,
    )
    labelled = labelled[:max_cues]
    return TrackProposal(
        stable_id=record.stable_id,
        backend_version=record.backend_version,
        cues=labelled,
    )


# ---------------------------------------------------------------------------
# Report writer
# ---------------------------------------------------------------------------


def write_json(proposals: list[TrackProposal], output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    doc: list[dict[str, Any]] = []
    for tp in proposals:
        doc.append({
            "stable_id": tp.stable_id,
            "backend_version": tp.backend_version,
            "cues": [
                {
                    "time_s": c.time_s,
                    "label": c.label,
                    "confidence": c.confidence,
                    "rms_dbfs": c.rms_dbfs,
                }
                for c in tp.cues
            ],
        })
    output.write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
    return output


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def run(
    *,
    records: Iterable[AnalysisRecord],
    limit: int | None = None,
    label_filter: set[str] | None = None,
    output: Path = DEFAULT_OUTPUT,
    db_path: Path | None = None,
    emit_events: bool = True,
) -> list[TrackProposal]:
    out: list[TrackProposal] = []
    for i, rec in enumerate(records):
        if limit is not None and i >= limit:
            break
        tp = propose_cues(rec)
        if label_filter:
            tp.cues = [c for c in tp.cues if c.label in label_filter]
        out.append(tp)
        if emit_events:
            publish_event(
                "cue.proposal",
                {
                    "stable_id": tp.stable_id,
                    "backend_version": tp.backend_version,
                    "n_cues": len(tp.cues),
                    "labels": [c.label for c in tp.cues],
                },
                stable_id=tp.stable_id,
                db_path=db_path,
            )
    write_json(out, output)
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m apps.analysis.auto_cues",
        description="Propose up to 8 labelled hot cues per track (META-04).",
    )
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--stable-ids", nargs="+", default=None)
    p.add_argument("--label-filter", nargs="+", default=None)
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
        from apps.analysis import store as _store
        rows = _store.fetch_records(backend=args.backend)
        records = [AnalysisRecord.from_json(r["record_json"]) for r in rows]

    proposals = run(
        records=records,
        limit=args.limit,
        label_filter=set(args.label_filter) if args.label_filter else None,
        output=args.output,
    )

    table = Table(title=f"cue proposals ({len(proposals)} tracks)")
    table.add_column("stable_id")
    table.add_column("#cues")
    table.add_column("labels")
    for tp in proposals[:20]:
        labels = "|".join(c.label or "-" for c in tp.cues)
        table.add_row(tp.stable_id[:18], str(len(tp.cues)), labels)
    console.print(table)
    console.print(f"[green]ok[/green] wrote {args.output}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
