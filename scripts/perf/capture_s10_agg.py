"""Pure aggregation for the S10 row-select prefetch ready KPI (PERF-CAPTURE-9701).

Input: snapshots of the page's perf event ring (``readPerfEvents()`` in
``apps/webui/frontend/src/lib/rb/perf-event-log.ts``), as a JSON list, the same shape
``capture_s8_agg`` reads: each item is a snapshot object ``{"perf_events": [...]}`` or a bare
list of events. A stored client performance sample
(``GET /api/v1/performance/telemetry/client-samples``) carries only ``perf_event_count``, so the
rows come from ring dumps taken next to those samples.

A row-select prefetch writes, 1 in ``sample_of`` times, a ``library-prefetch-anlz`` row
(``recordAnlzPrefetchSampled``: ``fetch_ms``, ``ok``, ``sample_of``) or a
``library-prefetch-audio`` row (``recordAudioPrefetchSampled``: ``fetch_ms``, ``mib``,
``sample_of``) in ``library-perf.ts``. Rows are de-duplicated by event identity across
overlapping snapshots. Rows with ``ok: 0`` are left out of the latency and reported as an error
rate; audio rows carry no ``ok`` because only a finished warm is recorded. The KPI is the
nearest-rank p95 of ``fetch_ms`` over every ready row, in seconds; the note carries the per-kind
p95. Under ``MIN_READY_ROWS`` ready rows the row is withheld, never a number.

    uv run python -m scripts.perf.capture_s10_agg --samples <snapshots.json> --dry-run
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

S10_KPI = "row_select_prefetch_ready_s"
S10_UNIT = "s"
ANLZ_KIND = "library-prefetch-anlz"
AUDIO_KIND = "library-prefetch-audio"
PREFETCH_KINDS = (ANLZ_KIND, AUDIO_KIND)
MIN_READY_ROWS = 10
S10_METHOD = (
    "perf event ring library-prefetch-anlz and library-prefetch-audio rows "
    "(recordAnlzPrefetchSampled, recordAudioPrefetchSampled), de-duplicated by event identity "
    "across snapshots; ok=0 rows excluded; nearest-rank p95 of fetch_ms in seconds"
)


def _stage(event: dict[str, Any], name: str) -> object:
    """One stage value of a timing row, or None when the row has no stages."""
    stages = event.get("stages")
    return stages.get(name) if isinstance(stages, dict) else None


def _fetch_s(event: dict[str, Any]) -> float:
    """A prefetch row's ready time in seconds; ValueError when ``fetch_ms`` is not a finite number."""
    value = _stage(event, "fetch_ms")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{event.get('kind')} row at {event.get('t')} has no finite fetch_ms: {event.get('stages')!r}")
    return float(value) / 1000.0


def is_error(event: dict[str, Any]) -> bool:
    """True when the row says the prefetch failed (``ok: 0``); rows without ``ok`` finished."""
    ok = _stage(event, "ok")
    return ok is not None and ok == 0


def sampling_note(rows: list[dict[str, Any]]) -> str:
    """The sampling factor the rows declare, e.g. ``sampled 1 in 5``."""
    factors = sorted({int(f) for f in (_stage(r, "sample_of") for r in rows) if isinstance(f, (int, float))})
    if not factors:
        return "sampling factor unknown (no sample_of)"
    return "sampled 1 in " + " or ".join(str(f) for f in factors)


def unique_prefetch_rows(snapshots: Iterable[object]) -> dict[str, list[dict[str, Any]]]:
    """Every prefetch row once per kind, in first-seen order."""
    materialised = list(snapshots)
    return {kind: unique_rows_of_kind(materialised, kind) for kind in PREFETCH_KINDS}


def _p95_text(values: list[float]) -> str:
    """A per-kind p95 for the note, or ``n/a`` when the kind has no ready rows."""
    return f"{nearest_rank_p95(values):g}s" if values else "n/a"


def snapshots_to_ledger_row(snapshots: Iterable[object], meta: CaptureMeta) -> dict[str, Any]:
    """One S10 ledger row: the overall p95 ready time, or a withheld row under ``MIN_READY_ROWS``."""
    by_kind = unique_prefetch_rows(snapshots)
    every = [row for kind in PREFETCH_KINDS for row in by_kind[kind]]
    method = f"{S10_METHOD}; {sampling_note(every)}"
    ready = {kind: [_fetch_s(row) for row in rows if not is_error(row)] for kind, rows in by_kind.items()}
    errors = sum(1 for row in every if is_error(row))
    error_rate = f"error rate {errors}/{len(every)}" + (f" ({100 * errors / len(every):.1f}%)" if every else "")
    values = ready[ANLZ_KIND] + ready[AUDIO_KIND]
    if len(values) < MIN_READY_ROWS:
        return withheld_row(
            kpi=S10_KPI,
            unit=S10_UNIT,
            method=method,
            meta=meta,
            reason=f"only {len(values)} of {MIN_READY_ROWS} ready prefetch rows needed for a p95; {error_rate}",
        )
    note = (
        f"n={len(values)} ready rows, nearest-rank p95; "
        f"anlz p95 {_p95_text(ready[ANLZ_KIND])} (n={len(ready[ANLZ_KIND])}); "
        f"audio p95 {_p95_text(ready[AUDIO_KIND])} (n={len(ready[AUDIO_KIND])}); {error_rate}"
    )
    return build_row(
        kpi=S10_KPI,
        value=nearest_rank_p95(values),
        unit=S10_UNIT,
        method=method,
        meta=meta,
        note=note,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI: print the S10 row and append it to the ledger unless ``--dry-run``."""
    parser = argparse.ArgumentParser(description="Aggregate S10 row-select prefetch timings into a KPI ledger row")
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
