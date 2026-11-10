"""Pure aggregation for S7 library filter keystroke KPIs (PERF-CAPTURE-11886).

Input: snapshots of the perf event ring as a JSON list. Each item is either
``{"perf_events": [...]}`` or a bare list of events.

Each settled local filter burst writes one ``library-filter`` row carrying
``series.keystroke_ms`` (keystroke to first rAF after recompute) and
``stages.dropped`` (keystrokes whose burst settled after a pane switch).

    uv run python -m scripts.perf.capture_s7_agg --samples <snapshots.json> --dry-run
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from scripts.perf.capture_kpi_ledger import (
    DEFAULT_LEDGER,
    CaptureMeta,
    append_entries,
    build_row,
    format_appended,
    git_sha,
    session_meta,
    withheld_row,
)
from scripts.perf.capture_s8_agg import nearest_rank_p95, unique_rows_of_kind

S7_EVENT_KIND = "library-filter"
S7_LATENCY_KPI = "library_filter_keystroke_ms_p95"
S7_LATENCY_UNIT = "ms p95"
S7_DROPPED_KPI = "library_filter_dropped_keystrokes_per_session"
S7_DROPPED_UNIT = "dropped keystrokes per session"
MIN_KEYSTROKES = 30
S7_METHOD = (
    "perf event ring library-filter rows (recordFilterTimingOnPaint / recordFilterDropped), "
    "de-duplicated by event identity across snapshots; nearest-rank p95 of "
    "series.keystroke_ms (keystroke to first rAF after the recompute) and the sum of stages.dropped"
)


def _finite_non_negative(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def keystroke_latencies(row: dict[str, Any]) -> list[float] | None:
    """Latencies from ``series.keystroke_ms``, or None for legacy rows without series."""
    series = row.get("series")
    if not isinstance(series, dict) or "keystroke_ms" not in series:
        return None
    raw = series["keystroke_ms"]
    if not isinstance(raw, list):
        raise TypeError(f"{S7_EVENT_KIND} row at {row.get('t')} has non-list keystroke_ms: {raw!r}")
    out: list[float] = []
    for item in raw:
        if not _finite_non_negative(item):
            raise ValueError(f"{S7_EVENT_KIND} row at {row.get('t')} has invalid keystroke_ms entry: {item!r}")
        out.append(float(item))
    return out


def dropped_count(row: dict[str, Any]) -> int:
    stages = row.get("stages")
    if not isinstance(stages, dict) or "dropped" not in stages:
        return 0
    value = stages["dropped"]
    if not _finite_non_negative(value):
        raise ValueError(f"{S7_EVENT_KIND} row at {row.get('t')} has invalid dropped: {value!r}")
    return int(value)


def snapshots_to_ledger_rows(snapshots: Iterable[object], meta: CaptureMeta) -> list[dict[str, Any]]:
    """Latency and dropped S7 ledger rows, or withheld rows under MIN_KEYSTROKES."""
    rows = unique_rows_of_kind(snapshots, S7_EVENT_KIND)
    legacy_skipped = 0
    latencies: list[float] = []
    dropped_total = 0
    for row in rows:
        ks = keystroke_latencies(row)
        if ks is None:
            legacy_skipped += 1
            continue
        latencies.extend(ks)
        dropped_total += dropped_count(row)

    if len(latencies) < MIN_KEYSTROKES:
        reason = f"only {len(latencies)} of {MIN_KEYSTROKES} keystroke latencies needed for a p95"
        return [
            withheld_row(kpi=S7_LATENCY_KPI, unit=S7_LATENCY_UNIT, method=S7_METHOD, meta=meta, reason=reason),
            withheld_row(kpi=S7_DROPPED_KPI, unit=S7_DROPPED_UNIT, method=S7_METHOD, meta=meta, reason=reason),
        ]

    legacy_note = f"; {legacy_skipped} legacy rows without keystroke_ms skipped" if legacy_skipped else ""
    latency_row = build_row(
        kpi=S7_LATENCY_KPI,
        value=nearest_rank_p95(latencies),
        unit=S7_LATENCY_UNIT,
        method=S7_METHOD,
        meta=meta,
        note=f"n={len(latencies)} keystrokes over {len(rows)} filter rows, nearest-rank p95{legacy_note}",
    )
    dropped_row = build_row(
        kpi=S7_DROPPED_KPI,
        value=float(dropped_total),
        unit=S7_DROPPED_UNIT,
        method=S7_METHOD,
        meta=meta,
        note=f"sum of dropped over {len(rows)} filter rows",
    )
    return [latency_row, dropped_row]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Aggregate S7 library filter timings into KPI ledger rows")
    parser.add_argument("--samples", required=True, type=Path, help="JSON list of perf event ring snapshots")
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--dry-run", action="store_true", help="print rows, append nothing")
    args = parser.parse_args(argv)

    snapshots = json.loads(args.samples.read_text(encoding="utf-8"))
    if not isinstance(snapshots, list):
        parser.error(f"{args.samples} must hold a JSON list of snapshots")
    rows = snapshots_to_ledger_rows(snapshots, session_meta(sha=git_sha()))
    for row in rows:
        print(format_appended(row))
    if args.dry_run:
        print(json.dumps(rows, indent=2))
        return 0
    append_entries(args.ledger, rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
