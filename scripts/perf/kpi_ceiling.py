"""Trailing ledger median and 3x ratcheting ceiling for warm anlz KPIs."""

from __future__ import annotations

import datetime as dt
import statistics
from typing import Any, Literal

SUPERSEDED = "SUPERSEDED"
DEFAULT_FACTOR = 3.0
CEILING_FACTOR = DEFAULT_FACTOR
DEFAULT_WINDOW_DAYS = 7

CeilingVerdict = Literal["unknown", "breach", "ok"]


def _parse_date(value: str) -> dt.date:
    return dt.date.fromisoformat(value)


def _live_values(
    entries: list[dict[str, Any]],
    kpi: str,
    today: dt.date,
    window_days: int,
) -> list[float]:
    start = today - dt.timedelta(days=window_days)
    values: list[float] = []
    for row in entries:
        if row.get("kpi") != kpi:
            continue
        if SUPERSEDED in str(row.get("note", "")):
            continue
        if row.get("status") == "error" or row.get("measured") is False:
            continue
        value = row.get("value")
        if not isinstance(value, (int, float)):
            continue
        date_raw = row.get("date")
        if not isinstance(date_raw, str):
            continue
        entry_date = _parse_date(date_raw)
        if start <= entry_date < today:
            values.append(float(value))
    return values


def trailing_median_ms(
    entries: list[dict[str, Any]],
    *,
    kpi: str,
    today: dt.date,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> float | None:
    values = _live_values(entries, kpi, today, window_days)
    if not values:
        return None
    return float(statistics.median(values))


def ceiling_verdict(
    *,
    warm_median_ms: float,
    trailing_median_ms: float | None,
    factor: float = DEFAULT_FACTOR,
) -> CeilingVerdict:
    if trailing_median_ms is None or trailing_median_ms <= 0:
        return "unknown"
    if warm_median_ms > trailing_median_ms * factor:
        return "breach"
    return "ok"


def ceiling_exceeded(
    *,
    warm_median_ms: float,
    trailing_median_ms: float | None,
    factor: float = DEFAULT_FACTOR,
) -> bool:
    return ceiling_verdict(
        warm_median_ms=warm_median_ms,
        trailing_median_ms=trailing_median_ms,
        factor=factor,
    ) == "breach"
