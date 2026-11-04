"""UTC timestamps as RFC3339 ``Z`` strings for analysis persistence."""

from __future__ import annotations

from datetime import UTC, datetime


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def datetime_to_utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    dt = dt.astimezone(UTC)
    return dt.isoformat().replace("+00:00", "Z")


__all__ = ["datetime_to_utc_iso", "utc_now_iso"]
