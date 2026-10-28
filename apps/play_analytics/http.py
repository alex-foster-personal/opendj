"""Delivery surface of ``apps.play_analytics``: the service factory and router.

Delivery layers (apps.webui, apps.engine_core, apps.opendj_cli) and scripts/
import this module or ``.contract``; nothing else outside the package may.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from apps.sets.paths import SETS_DB

from .contract import (
    DEFAULT_MIN_AUDIBLE_S,
    AnalyticsSchemaError,
    EventsTable,
    PlayAnalyticsService,
    ShareState,
)
from .service import PlayAnalytics


def build_service() -> PlayAnalyticsService:
    """Return the default play-analytics service."""
    return PlayAnalytics()


def create_router(*, db_path: Path = SETS_DB, events_table: EventsTable = "events") -> APIRouter:
    """Create a router bound to one explicit event-store path.

    Integration intentionally remains separate from the shared server app so a
    coordinating parity PR can register this router without hotspot conflicts.
    """
    service = build_service()
    router = APIRouter(prefix="/api/play-analytics", tags=["play-analytics"])

    @router.get("")
    def get_play_analytics(
        share_state: ShareState | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        min_audible_s: Annotated[
            float,
            Query(
                ge=0,
                description=(
                    "Read-time 'counts as played' filter, in seconds of "
                    "audible playback. Only applies to rows that recorded a "
                    "dwell (Open DJ's own decks). 0 counts every play."
                ),
            ),
        ] = DEFAULT_MIN_AUDIBLE_S,
    ) -> dict[str, object]:
        try:
            return dict(
                service.query_play_analytics(
                    db_path,
                    share_state=share_state,
                    limit=limit,
                    events_table=events_table,
                    min_audible_s=min_audible_s,
                )
            )
        except AnalyticsSchemaError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    return router


router = create_router()

__all__ = [
    "DEFAULT_MIN_AUDIBLE_S",
    "AnalyticsSchemaError",
    "build_service",
    "create_router",
    "router",
]
