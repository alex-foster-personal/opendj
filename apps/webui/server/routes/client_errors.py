"""Bounded browser-error sink with full details in a separate daily log."""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, get_args

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from apps.shared.telemetry import capture_browser_error, scrub_bug_report_mapping
from apps.shared.telemetry.error_id import stable_error_id
from apps.shared.telemetry.sink import (
    client_error_message,
    client_source_site,
    current_build_sha,
    current_host,
)

from ..client_logs import DEFAULT_LOG_DIR, append_json_record, daily_log_path

router = APIRouter(prefix="/client-errors", tags=["client-errors"])
MOUNT_UNDER_API_PREFIX = True
log = logging.getLogger(__name__)

ContextValue = str | int | float | bool | None


ClientErrorKind = Literal[
    "window-error",
    "unhandled-rejection",
    "sveltekit",
    "ui-error",
    "console-error",
    "console-warn",
    "resource-error",
    "csp-violation",
    "webview-console",
    "webview-navigation",
]
CLIENT_ERROR_KINDS: tuple[str, ...] = get_args(ClientErrorKind)
#: Most records one filtered read returns (AGENT-8304).
CLIENT_ERRORS_MAX_LIMIT = 1000


class ClientErrorIn(BaseModel):
    client_event_id: str = Field(min_length=1, max_length=128)
    kind: ClientErrorKind
    message: str = Field(min_length=1, max_length=4096)
    name: str | None = Field(default=None, max_length=256)
    stack: str | None = Field(default=None, max_length=32768)
    url: str = Field(max_length=4096)
    client_timestamp: str = Field(max_length=128)
    user_agent: str = Field(max_length=2048)
    secure_context: bool
    audio_worklet_available: bool
    #: The page's own transport read at the moment the error fired: True when
    #: any deck was playing or audible. It gates the Sentry forward (never the
    #: local log). None is a client that predates the field, which falls back
    #: to the engine's UI-mirror probe. See apps/shared/telemetry/live.py.
    any_deck_live: bool | None = None
    context: dict[str, ContextValue] = Field(default_factory=dict, max_length=32)

    @field_validator("context")
    @classmethod
    def validate_context(cls, value: dict[str, ContextValue]) -> dict[str, ContextValue]:
        for key, item in value.items():
            if len(key) > 128:
                raise ValueError("context keys must be at most 128 characters")
            if isinstance(item, str) and len(item) > 4096:
                raise ValueError("context strings must be at most 4096 characters")
        return value


class ClientErrorOut(BaseModel):
    event_id: str
    stored: bool
    error_id: str | None = None
    sentry_event_id: str | None = None


class ClientErrorTriageIn(BaseModel):
    disposition: Literal["fix", "no-fix", "duplicate"]
    ref: str = Field(min_length=1, max_length=4096)


class ClientErrorTriageOut(ClientErrorTriageIn):
    event_id: str


class ClientErrorRecord(BaseModel):
    event_id: str
    received_at: str
    kind: str
    message: str
    stack: str | None = None
    url: str | None = None
    name: str | None = None
    context: dict[str, ContextValue] | None = None
    build_sha: str | None = None


def _daily_logs(log_dir: Path) -> list[Path]:
    return sorted(log_dir.glob("webui-client-errors-????-??-??.log"))


def _triage_path(daily_path: Path) -> Path:
    return daily_path.with_suffix(".triage.jsonl")


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _triaged_event_ids(log_dir: Path) -> set[str]:
    return {
        str(record["event_id"])
        for daily_path in _daily_logs(log_dir)
        for record in _read_jsonl(_triage_path(daily_path))
    }


def _find_event_log(log_dir: Path, event_id: str) -> Path | None:
    for daily_path in _daily_logs(log_dir):
        if any(record.get("event_id") == event_id for record in _read_jsonl(daily_path)):
            return daily_path
    return None


def _log_dir(request: Request) -> Path:
    return Path(getattr(request.app.state, "client_error_log_dir", DEFAULT_LOG_DIR))


def bug_report_client_error_row(record: Mapping[str, object]) -> dict[str, object]:
    """One client-error row with the bug-report allowlist (issue #11173)."""
    payload = {
        "event_id": record.get("event_id"),
        "received_at": record.get("received_at"),
        "kind": record.get("kind"),
        "message": record.get("message"),
        "name": record.get("name"),
        "build_sha": record.get("build_sha"),
        "error_id": record.get("error_id"),
        "any_deck_live": record.get("any_deck_live"),
        "secure_context": record.get("secure_context"),
        "audio_worklet_available": record.get("audio_worklet_available"),
    }
    return scrub_bug_report_mapping(payload)


@router.post("", response_model=ClientErrorOut, status_code=202)
def capture_client_error(payload: ClientErrorIn, request: Request) -> ClientErrorOut:
    event_id = uuid.uuid4().hex[:16]
    received_at = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    source_site = client_source_site(payload.kind, payload.url)
    error_message = client_error_message(payload.name, payload.message)
    record = {
        "event_id": event_id,
        "received_at": received_at,
        "client_ip": request.client.host if request.client else None,
        **payload.model_dump(),
        "error_id": stable_error_id(source_site=source_site, message=error_message),
        "host": current_host(),
        "build_sha": current_build_sha(),
    }
    log_dir = _log_dir(request)
    path = daily_log_path(log_dir, "webui-client-errors", time.gmtime())
    stored = append_json_record(path, record)
    summary = payload.message.replace("\n", " ")[:300]
    # WARNING, not ERROR: capture_browser_error below already reports this
    # error. At ERROR, warning_log's forward sent it to Sentry a second time.
    if stored:
        log.warning(
            "browser error %s %s: %s (details: %s)",
            event_id,
            payload.kind,
            summary,
            path,
        )
    else:
        log.warning(
            "browser error %s not stored: daily log cap reached at %s (%s)",
            event_id,
            path,
            summary,
        )
    # Forward to Sentry AFTER the local log is written. The daily log is the
    # record that must not be lost; telemetry is remote aggregation on top of
    # it, and it is a no-op whenever telemetry is off (every dev checkout).
    # The browser does not report to Sentry itself -- see capture_browser_error
    # for why the engine owns this -- so this call is the only path a client
    # error has to an issue.
    sentry_event_id = capture_browser_error(
        message=payload.message,
        name=payload.name,
        stack=payload.stack,
        url=payload.url,
        user_agent=payload.user_agent,
        # The page's free-form context goes in FIRST so the typed fields win:
        # `any_deck_live` is what the live-set gate reads, and a context key
        # of the same name must not be able to overwrite it (Codex, #3737).
        context={
            **payload.context,
            "kind": payload.kind,
            "client_event_id": payload.client_event_id,
            "secure_context": payload.secure_context,
            "audio_worklet_available": payload.audio_worklet_available,
            "any_deck_live": payload.any_deck_live,
        },
    )
    return ClientErrorOut(
        event_id=event_id,
        stored=stored,
        error_id=str(record["error_id"]),
        sentry_event_id=sentry_event_id,
    )


def _parse_since(since: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(since.replace("Z", "+00:00"))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=f"since must be an ISO 8601 time, got {since!r}") from error
    if parsed.tzinfo is None:
        raise HTTPException(status_code=422, detail=f"since must carry a timezone (Z or +hh:mm), got {since!r}")
    return parsed


def _matches(
    record: dict[str, object],
    *,
    since: datetime | None,
    build_sha: str | None,
    kind: str | None,
    contains: str | None,
) -> bool:
    if since is not None:
        received = datetime.fromisoformat(str(record["received_at"]).replace("Z", "+00:00"))
        if received < since:
            return False
    if build_sha is not None and not str(record.get("build_sha") or "").startswith(build_sha):
        return False
    if kind is not None and record.get("kind") != kind:
        return False
    return contains is None or contains.casefold() in str(record.get("message", "")).casefold()


@router.get("", response_model=list[ClientErrorRecord])
def list_client_errors(
    request: Request,
    untriaged: bool = Query(default=False),
    since: str | None = Query(default=None, description="ISO 8601 time; records received at or after it"),
    build_sha: str | None = Query(default=None, min_length=1, description="build_sha prefix"),
    kind: str | None = Query(default=None, description="exact client error kind"),
    contains: str | None = Query(default=None, min_length=1, description="case-insensitive message substring"),
    limit: int | None = Query(default=None, ge=1, le=CLIENT_ERRORS_MAX_LIMIT, description="newest N, oldest first"),
) -> list[ClientErrorRecord]:
    """Every stored client error, oldest first, optionally filtered (AGENT-8304)."""
    if kind is not None and kind not in CLIENT_ERROR_KINDS:
        raise HTTPException(
            status_code=422,
            detail=f"kind must be one of {', '.join(CLIENT_ERROR_KINDS)}, got {kind!r}",
        )
    since_at = _parse_since(since) if since is not None else None
    log_dir = _log_dir(request)
    triaged_ids = _triaged_event_ids(log_dir) if untriaged else set()
    records = [
        ClientErrorRecord.model_validate(record)
        for daily_path in _daily_logs(log_dir)
        for record in _read_jsonl(daily_path)
        if (not untriaged or str(record["event_id"]) not in triaged_ids)
        and _matches(record, since=since_at, build_sha=build_sha, kind=kind, contains=contains)
    ]
    return records[-limit:] if limit is not None else records


@router.patch("/{event_id}", response_model=ClientErrorTriageOut)
def triage_client_error(event_id: str, payload: ClientErrorTriageIn, request: Request) -> ClientErrorTriageOut:
    log_dir = _log_dir(request)
    daily_path = _find_event_log(log_dir, event_id)
    if daily_path is None:
        raise HTTPException(status_code=404, detail=f"Unknown client error event: {event_id}")
    record = {"event_id": event_id, **payload.model_dump()}
    if not append_json_record(_triage_path(daily_path), record):
        raise HTTPException(status_code=507, detail="Client error triage log is full")
    return ClientErrorTriageOut(event_id=event_id, **payload.model_dump())
