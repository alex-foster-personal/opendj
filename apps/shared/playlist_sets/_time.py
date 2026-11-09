"""UTC ISO timestamps for playlist-set persistence."""

from __future__ import annotations

from datetime import UTC, datetime


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
