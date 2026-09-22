"""Append-only KPI ledger rows for BOOT-LIB open-to-library-rows capture."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Any

CAPTURE_ID = "perf-capture"
ROUND = "issue-2697"
SOURCE = (
    "client perf-span open-to-library-rows from navigationStart to first track row paint"
)
METHOD = "navigationStart to first track row first paint"
SPAN_NAME = "open-to-library-rows"
MISSING_TELEMETRY_PREFIX = "missing-telemetry:"


def classify_boot_library_withhold_reason(reason: str | None) -> str:
    if reason is None:
        return (
            f"{MISSING_TELEMETRY_PREFIX} no perf-span POST "
            "(open-to-library-rows telemetry absent or library did not reach first paint)"
        )
    if reason.startswith(MISSING_TELEMETRY_PREFIX):
        return reason
    return reason


def _is_finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)


def _span_is_scorable(span: dict[str, Any]) -> tuple[bool, str | None]:
    if span.get("kind") != "perf-span":
        return False, "span kind is not perf-span"
    if span.get("name") != SPAN_NAME:
        return False, f"span name is not {SPAN_NAME}"
    duration_ms = span.get("duration_ms")
    if not _is_finite_number(duration_ms) or duration_ms < 0:
        return False, "duration_ms is not a finite non-negative number"
    return True, None


@dataclass(frozen=True)
class _LedgerRowCtx:
    sha: str
    machine: str
    capture_date: date
    note: str
    status: str | None = None
    measured: bool | None = None


def _base_row(
    kpi: str,
    value: float | None,
    unit: str,
    ctx: _LedgerRowCtx,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "date": ctx.capture_date.isoformat(),
        "round": ROUND,
        "kpi": kpi,
        "value": value,
        "unit": unit,
        "machine": ctx.machine,
        "source": SOURCE,
        "method": METHOD,
        "sha": ctx.sha,
        "capture_id": CAPTURE_ID,
        "note": ctx.note,
    }
    if ctx.status is not None:
        row["status"] = ctx.status
    if ctx.measured is not None:
        row["measured"] = ctx.measured
    return row


def span_to_ledger_rows(
    span: dict[str, Any] | None,
    *,
    sha: str,
    machine: str,
    capture_date: date,
    reason: str | None = None,
) -> list[dict[str, Any]]:
    """Convert a client-events perf-span into a BOOT-LIB ledger row."""
    if span is None:
        withheld_reason = classify_boot_library_withhold_reason(reason)
        note = f"UNKNOWN: {withheld_reason}"
        ctx = _LedgerRowCtx(
            sha=sha,
            machine=machine,
            capture_date=capture_date,
            note=note,
            status="withheld",
            measured=False,
        )
        return [_base_row("open_to_library_rows_ms", None, "ms", ctx)]
    ok, reject_reason = _span_is_scorable(span)
    if not ok:
        ctx = _LedgerRowCtx(
            sha=sha,
            machine=machine,
            capture_date=capture_date,
            note=f"UNKNOWN: {reject_reason}",
            status="withheld",
            measured=False,
        )
        return [_base_row("open_to_library_rows_ms", None, "ms", ctx)]
    duration_ms = float(span["duration_ms"])
    ctx = _LedgerRowCtx(
        sha=sha,
        machine=machine,
        capture_date=capture_date,
        note="measured open-to-library-rows perf-span",
        measured=True,
    )
    return [_base_row("open_to_library_rows_ms", duration_ms, "ms", ctx)]
