"""UTC ISO-8601 stamps shared by engine_core persistence and wire envelopes."""

from __future__ import annotations

from datetime import UTC, datetime


def utc_iso_ms() -> str:
    """Wall-clock UTC with millisecond precision (no ``Z`` suffix)."""
    return datetime.now(UTC).isoformat(timespec="milliseconds")


__all__ = ["utc_iso_ms"]
