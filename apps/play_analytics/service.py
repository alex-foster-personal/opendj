"""Default ``PlayAnalyticsService``: delegates to :mod:`.query` unchanged."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .contract import DEFAULT_MIN_AUDIBLE_S, EventsTable, ShareState
from .query import query_play_analytics


class PlayAnalytics:
    """Implements ``apps.play_analytics.contract.PlayAnalyticsService``."""

    def query_play_analytics(
        self,
        db_path: Path,
        *,
        share_state: ShareState | None = None,
        limit: int = 50,
        events_table: EventsTable = "events",
        min_audible_s: float = DEFAULT_MIN_AUDIBLE_S,
    ) -> dict[str, Any]:
        return query_play_analytics(
            db_path,
            share_state=share_state,
            limit=limit,
            events_table=events_table,
            min_audible_s=min_audible_s,
        )


__all__ = ["PlayAnalytics"]
