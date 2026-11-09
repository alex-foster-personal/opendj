"""Pure aggregation for agent order round-trip KPIs (AGENT-10047, issue #10047).

Input: the ``GET /api/v1/commands/timings`` document from a live engine or a
saved JSON copy. Emits ``agent_order_round_trip_ms_p95`` and
``agent_order_claim_ms_p95``; under ``MIN_ORDERS`` answered orders each row is
withheld.

    uv run python -m scripts.perf.capture_order_rtt --engine http://127.0.0.1:<port> --dry-run
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

from scripts.perf.capture_kpi_ledger import (
    DEFAULT_LEDGER,
    CaptureMeta,
    append_entries,
    build_row,
    fetch_url,
    format_appended,
    git_sha,
    session_meta,
    withheld_row,
)

ROUND_TRIP_KPI = "agent_order_round_trip_ms_p95"
CLAIM_KPI = "agent_order_claim_ms_p95"
UNIT = "ms"
MIN_ORDERS = 20
TIMINGS_PATH = "/api/v1/commands/timings"
FETCH_TIMEOUT_S = 10.0
METHOD = (
    "GET /api/v1/commands/timings on the broker ring (last 200 answered orders); "
    "nearest-rank p95; scripts.perf.capture_order_rtt"
)


def fetch_timings(engine: str, timeout: float = FETCH_TIMEOUT_S) -> dict[str, Any]:
    url = engine.rstrip("/") + TIMINGS_PATH
    status, _headers, body = fetch_url(url, timeout)
    if status != 200:
        raise ValueError(f"{url} returned HTTP {status}")
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{url} body was not JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise TypeError(f"{url} body was not a JSON object")
    return document


def _p95(document: dict[str, Any], field: str) -> float:
    section = document.get(field)
    if not isinstance(section, dict):
        raise TypeError(f"timings document missing {field!r} spread")
    value = section.get("p95")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"timings document {field}.p95 is not a finite non-negative number: {value!r}")
    return float(value)


def _by_type_note(document: dict[str, Any]) -> str:
    by_type = document.get("by_type")
    if not isinstance(by_type, dict) or not by_type:
        return ""
    parts: list[str] = []
    for key in sorted(by_type):
        bucket = by_type[key]
        if not isinstance(bucket, dict):
            continue
        total = bucket.get("total_ms")
        if not isinstance(total, dict):
            continue
        p95 = total.get("p95")
        if isinstance(p95, (int, float)) and math.isfinite(p95):
            parts.append(f"{key}={p95}")
    if not parts:
        return ""
    return "; by_type p95 ms: " + ", ".join(parts)


def _load_timings_from_file(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise TypeError(f"{path} must hold a JSON object")
    return document


def timings_to_ledger_rows(document: dict[str, Any], meta: CaptureMeta) -> list[dict[str, Any]]:
    count = document.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError(f"timings document count must be a non-negative int, got {count!r}")
    if count < MIN_ORDERS:
        reason = f"only {count} of {MIN_ORDERS} orders needed for a p95"
        return [
            withheld_row(kpi=ROUND_TRIP_KPI, unit=UNIT, method=METHOD, meta=meta, reason=reason),
            withheld_row(kpi=CLAIM_KPI, unit=UNIT, method=METHOD, meta=meta, reason=reason),
        ]
    note_base = f"n={count} answered orders, nearest-rank p95"
    extra = _by_type_note(document)
    note = note_base + extra
    return [
        build_row(
            kpi=ROUND_TRIP_KPI,
            value=_p95(document, "total_ms"),
            unit=UNIT,
            method=METHOD,
            meta=meta,
            note=note,
        ),
        build_row(
            kpi=CLAIM_KPI,
            value=_p95(document, "claim_ms"),
            unit=UNIT,
            method=METHOD,
            meta=meta,
            note=note,
        ),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Aggregate agent order round-trip timings into KPI ledger rows")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--engine", help="engine base URL (http://host:port)")
    source.add_argument("--timings", type=Path, help="saved JSON copy of GET /commands/timings")
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--dry-run", action="store_true", help="print the rows, append nothing")
    args = parser.parse_args(argv)

    try:
        if args.timings is not None:
            document = _load_timings_from_file(args.timings)
        else:
            assert args.engine is not None
            document = fetch_timings(args.engine)
    except (TypeError, ValueError, OSError) as exc:
        print(f"capture_order_rtt: {exc}", file=sys.stderr)
        return 2

    rows = timings_to_ledger_rows(document, session_meta(sha=git_sha()))
    for row in rows:
        print(format_appended(row))
    if args.dry_run:
        print(json.dumps(rows, indent=2))
        return 0
    append_entries(args.ledger, rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
