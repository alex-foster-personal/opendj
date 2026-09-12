"""Central error feed across server-readable diagnostic sinks."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from apps.webui.server.client_logs import DEFAULT_LOG_DIR
from apps.webui.server.error_feed import (
    ContextValue,
    collect_error_feed,
    default_window,
)

router = APIRouter(prefix="/errors", tags=["errors"])


class ErrorFeedEvent(BaseModel):
    source: str
    received_at: str
    level: str
    kind: str
    message: str
    stack: str | None = None
    url: str | None = None
    context: dict[str, ContextValue] | None = None
    event_id: str | None = None


class ErrorFeedSink(BaseModel):
    id: str
    available: bool
    roots: int | None = None
    files: int | None = None
    unreadable_lines: int | None = None
    reason: str | None = None


class ErrorFeedOut(BaseModel):
    since: str
    until: str
    truncated: bool
    sinks: list[ErrorFeedSink]
    events: list[ErrorFeedEvent] = Field(default_factory=list)


def _parse_query_time(value: str | None, default: datetime) -> datetime:
    if value is None:
        return default
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid timestamp: {value}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


@router.get("", response_model=ErrorFeedOut)
def list_errors(
    request: Request,
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
) -> ErrorFeedOut:
    default_since, default_until = default_window()
    since_dt = _parse_query_time(since, default_since)
    until_dt = _parse_query_time(until, default_until)
    client_error_log_dir = getattr(
        request.app.state, "client_error_log_dir", DEFAULT_LOG_DIR
    )
    legacy_dir = getattr(
        request.app.state, "client_error_legacy_log_dir", DEFAULT_LOG_DIR
    )
    performance_log_dir = getattr(request.app.state, "performance_log_dir", None)
    ui_mirror: dict[str, Any] | None = getattr(request.app.state, "ui_mirror", None)

    try:
        result = collect_error_feed(
            since=since_dt,
            until=until_dt,
            client_error_log_dir=client_error_log_dir,
            client_error_legacy_log_dir=legacy_dir,
            performance_log_dir=performance_log_dir,
            ui_mirror=ui_mirror,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return ErrorFeedOut(
        since=result.since,
        until=result.until,
        truncated=result.truncated,
        sinks=[ErrorFeedSink.model_validate(sink.__dict__) for sink in result.sinks],
        events=[ErrorFeedEvent.model_validate(event.__dict__) for event in result.events],
    )
