"""UTC ISO timestamps for play-order persistence and events."""
from __future__ import annotations

from datetime import UTC, datetime


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
