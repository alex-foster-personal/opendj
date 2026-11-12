"""KPI ledger rows for the S9/S11 first-browse capture (issue #10240)."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from scripts.perf.capture_kpi_ledger import CaptureMeta, build_row, withheld_row

S11_KPI = "cold_start_to_first_browse_s"
S9_KPI = "all_tracks_time_to_interactive_s"
UNIT = "s"
S11_METHOD = "navigationStart to first accepted row click"
S9_METHOD = "first row paint to first accepted row click"
SHELL_CAVEAT = "excludes shell launch before navigationStart"


def _is_finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _s11_note(first_browse: Mapping[str, Any]) -> str:
    clicks = first_browse.get("clicks", 0)
    rejected = first_browse.get("rejected_attempts", 0)
    return f"{SHELL_CAVEAT}; clicks={clicks}; rejected_attempts={rejected}"


def _s9_note(first_browse: Mapping[str, Any]) -> str:
    open_ms = first_browse.get("open_to_rows_ms")
    base = _s11_note(first_browse)
    if _is_finite_number(open_ms):
        return f"{base}; open_to_rows_ms={open_ms}"
    return base


def first_browse_to_ledger_rows(
    first_browse: Mapping[str, Any] | None,
    *,
    meta: CaptureMeta,
    reason: str | None = None,
) -> list[dict[str, Any]]:
    """Return exactly two ledger rows: S11 then S9."""
    fb: Mapping[str, Any] = first_browse or {}
    nav_ms = fb.get("nav_start_to_click_ms")
    rows_ms = fb.get("rows_paint_to_click_ms")
    span_reason = fb.get("span_reason")
    if isinstance(span_reason, str):
        span_reason = span_reason.strip() or None
    else:
        span_reason = None

    if _is_finite_number(nav_ms) and nav_ms >= 0:
        s11 = build_row(
            kpi=S11_KPI,
            value=float(nav_ms) / 1000.0,
            unit=UNIT,
            method=S11_METHOD,
            meta=meta,
            note=_s11_note(fb),
            measured=True,
        )
    else:
        withheld = reason or "timeout: no accepted row click"
        if not withheld.startswith("UNKNOWN:"):
            withheld = f"UNKNOWN: {withheld}"
        s11 = withheld_row(kpi=S11_KPI, unit=UNIT, method=S11_METHOD, meta=meta, reason=withheld)

    if _is_finite_number(rows_ms) and rows_ms >= 0:
        s9 = build_row(
            kpi=S9_KPI,
            value=float(rows_ms) / 1000.0,
            unit=UNIT,
            method=S9_METHOD,
            meta=meta,
            note=_s9_note(fb),
            measured=True,
        )
    else:
        if span_reason:
            w_reason = span_reason if span_reason.startswith("UNKNOWN:") else f"UNKNOWN: {span_reason}"
        elif reason:
            w_reason = reason if reason.startswith("UNKNOWN:") else f"UNKNOWN: {reason}"
        else:
            w_reason = "UNKNOWN: missing open-to-library-rows perf-span"
        s9 = withheld_row(kpi=S9_KPI, unit=UNIT, method=S9_METHOD, meta=meta, reason=w_reason)

    return [s11, s9]
