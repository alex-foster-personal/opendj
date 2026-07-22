"""Mountable FastAPI router for the read-only play-analytics contract."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from apps.sets.paths import SETS_DB

from .query import AnalyticsSchemaError, EventsTable, ShareState, query_play_analytics


def create_router(
    *, db_path: Path = SETS_DB, events_table: EventsTable = "events"
) -> APIRouter:
    """Create a router bound to one explicit event-store path.

    Integration intentionally remains separate from the shared server app so a
    coordinating parity PR can register this router without hotspot conflicts.
    """
    router = APIRouter(prefix="/api/play-analytics", tags=["play-analytics"])

    @router.get("")
    def get_play_analytics(
        share_state: ShareState | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> dict[str, object]:
        try:
            return query_play_analytics(
                db_path,
                share_state=share_state,
                limit=limit,
                events_table=events_table,
            )
        except AnalyticsSchemaError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    return router


router = create_router()

__all__ = ["create_router", "router"]
