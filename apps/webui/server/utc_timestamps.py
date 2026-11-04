"""UTC ISO-8601 timestamps for webui server responses."""
from __future__ import annotations

from datetime import UTC, datetime


def utc_now_iso_ms() -> str:
    """Current UTC time as ``YYYY-MM-DDTHH:MM:SS.mmmZ``."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
