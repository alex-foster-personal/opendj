"""Pure aggregation for the S8 whole-collection search round trip KPI (PERF-CAPTURE-9959).

Input: snapshots of the page's perf event ring (``readPerfEvents()`` in
``apps/webui/frontend/src/lib/rb/perf-event-log.ts``), as a JSON list. Each item is either a
snapshot object ``{"perf_events": [...]}`` or a bare list of events. The server-side client
performance sample (``POST /api/v1/performance/telemetry/client-samples``) carries only
``perf_event_count``, not the events, so the events come from a ring dump.

Each completed FTS query writes one ``library-search-collection`` row
(``recordCollectionSearchTiming`` in ``library-perf.ts``) whose ``stages.query_ms`` is the round
trip. Overlapping snapshots repeat rows, so rows are de-duplicated by event identity: the row's
``id`` when it has one, else its ``(t, kind, message)`` (timing rows carry no ``id``; ``t`` is the
ISO time it was written). The KPI is the nearest-rank p95 of ``query_ms``; under
``MIN_QUERIES`` queries the row is withheld, never a number.

    uv run python -m scripts.perf.capture_s8_agg --samples <snapshots.json> --dry-run
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

S8_KPI = "collection_search_round_trip_ms_p95"
S8_UNIT = "ms"
S8_EVENT_KIND = "library-search-collection"
MIN_QUERIES = 20
S8_METHOD = (
    "perf event ring library-search-collection rows (recordCollectionSearchTiming), "
    "de-duplicated by event identity across snapshots; nearest-rank p95 of query_ms"
)


# ----------------------------------------------------------------------------- reading


def _events_of(snapshot: object) -> list[Any]:
    if isinstance(snapshot, list):
        return snapshot
    if isinstance(snapshot, dict) and isinstance(snapshot.get("perf_events"), list):
        return snapshot["perf_events"]
    raise ValueError(
        f"a snapshot is a list of perf events or an object with perf_events, got {type(snapshot).__name__}"
    )


def _identity(event: dict[str, Any]) -> tuple[str, ...]:
    event_id = event.get("id")
    if isinstance(event_id, str) and event_id:
        return ("id", event_id)
    return ("row", str(event.get("t")), str(event.get("kind")), str(event.get("message")))


def _query_ms(event: dict[str, Any]) -> float:
    stages = event.get("stages")
    value = stages.get("query_ms") if isinstance(stages, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{S8_EVENT_KIND} row at {event.get('t')} has no finite query_ms: {stages!r}")
    return float(value)


def unique_rows_of_kind(snapshots: Iterable[object], kind: str) -> list[dict[str, Any]]:
    """Every row of ``kind`` once, in first-seen order."""
    seen: set[tuple[str, ...]] = set()
    rows: list[dict[str, Any]] = []
    for snapshot in snapshots:
        for event in _events_of(snapshot):
            if not isinstance(event, dict) or event.get("kind") != kind:
                continue
            key = _identity(event)
            if key in seen:
                continue
            seen.add(key)
            rows.append(event)
    return rows


def unique_search_rows(snapshots: Iterable[object]) -> list[dict[str, Any]]:
    """Every ``library-search-collection`` row once, in first-seen order."""
    return unique_rows_of_kind(snapshots, S8_EVENT_KIND)


# ----------------------------------------------------------------------------- scoring


def nearest_rank_p95(values: list[float]) -> float:
    """Nearest-rank p95: the value at rank ceil(0.95 * n)."""
    if not values:
        raise ValueError("p95 of no values")
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def snapshots_to_ledger_row(snapshots: Iterable[object], meta: CaptureMeta) -> dict[str, Any]:
    """One S8 ledger row: the p95, or a withheld row under ``MIN_QUERIES`` queries."""
    rows = unique_search_rows(snapshots)
    if len(rows) < MIN_QUERIES:
        return withheld_row(
            kpi=S8_KPI,
            unit=S8_UNIT,
            method=S8_METHOD,
            meta=meta,
            reason=f"only {len(rows)} of {MIN_QUERIES} queries needed for a p95",
        )
    values = [_query_ms(row) for row in rows]
    return build_row(
        kpi=S8_KPI,
        value=nearest_rank_p95(values),
        unit=S8_UNIT,
        method=S8_METHOD,
        meta=meta,
        note=f"n={len(values)} unique queries, nearest-rank p95",
    )


# ----------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Aggregate S8 collection search timings into a KPI ledger row")
    parser.add_argument("--samples", required=True, type=Path, help="JSON list of perf event ring snapshots")
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--dry-run", action="store_true", help="print the row, append nothing")
    args = parser.parse_args(argv)

    snapshots = json.loads(args.samples.read_text(encoding="utf-8"))
    if not isinstance(snapshots, list):
        parser.error(f"{args.samples} must hold a JSON list of snapshots")
    row = snapshots_to_ledger_row(snapshots, session_meta(sha=git_sha()))
    print(format_appended(row))
    if args.dry_run:
        print(json.dumps(row, indent=2))
        return 0
    append_entries(args.ledger, [row])
    return 0


if __name__ == "__main__":
    sys.exit(main())
