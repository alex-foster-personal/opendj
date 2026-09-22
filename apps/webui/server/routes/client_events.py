"""Privacy-bounded page-view sink for local and remotely served web UIs."""

from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, field_validator

from ..client_logs import DEFAULT_LOG_DIR, append_json_record, daily_log_path

router = APIRouter(prefix="/client-events", tags=["client-events"])
log = logging.getLogger(__name__)

_PERF_SPAN_NAMES: frozenset[str] = frozenset(
    {"login-submit-to-library-usable", "open-to-library-rows"}
)


class PageViewIn(BaseModel):
    client_event_id: str = Field(min_length=1, max_length=128)
    kind: Literal["page-view"]
    url: str = Field(min_length=1, max_length=4096)
    path: str = Field(min_length=1, max_length=2048)
    referrer: str | None = Field(default=None, max_length=4096)
    client_timestamp: str = Field(max_length=128)
    user_agent: str = Field(max_length=2048)
    language: str | None = Field(default=None, max_length=128)
    secure_context: bool
    viewport_width: int = Field(ge=0, le=100_000)
    viewport_height: int = Field(ge=0, le=100_000)

    @field_validator("path")
    @classmethod
    def path_must_be_absolute(cls, value: str) -> str:
        if not value.startswith("/") or "?" in value or "#" in value:
            raise ValueError("path must be absolute and exclude query/fragment")
        return value

    @field_validator("url", "referrer")
    @classmethod
    def urls_exclude_secrets(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("URL must be an absolute HTTP(S) URL")
        if parsed.query or parsed.fragment:
            raise ValueError("visitor URLs must exclude query and fragment")
        return value


class PerfSpanIn(BaseModel):
    client_event_id: str = Field(min_length=1, max_length=128)
    kind: Literal["perf-span"]
    name: str = Field(min_length=1, max_length=128)
    duration_ms: float = Field(ge=0, le=86_400_000)
    method: str = Field(min_length=1, max_length=256)
    stages: dict[str, float] | None = None
    client_timestamp: str = Field(max_length=128)

    @field_validator("name")
    @classmethod
    def name_must_be_allowlisted(cls, value: str) -> str:
        if value not in _PERF_SPAN_NAMES:
            raise ValueError(f"perf-span name {value!r} is not allowlisted")
        return value


ClientEventIn = Annotated[PageViewIn | PerfSpanIn, Field(discriminator="kind")]


class ClientEventOut(BaseModel):
    event_id: str
    stored: bool


@router.post("", response_model=ClientEventOut, status_code=202)
def capture_client_event(payload: ClientEventIn, request: Request) -> ClientEventOut:
    event_id = uuid.uuid4().hex[:16]
    received_at = datetime.now(UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
    record = {
        "event_id": event_id,
        "received_at": received_at,
        "client_ip": request.client.host if request.client else None,
        "forwarded_for": request.headers.get("x-forwarded-for"),
        "tailscale_user_login": request.headers.get("tailscale-user-login"),
        "tailscale_user_name": request.headers.get("tailscale-user-name"),
        "cloudflare_access_email": request.headers.get(
            "cf-access-authenticated-user-email"
        ),
        "request_host": request.headers.get("host"),
        **payload.model_dump(),
    }
    log_dir = Path(
        getattr(request.app.state, "client_event_log_dir", DEFAULT_LOG_DIR)
    )
    path = daily_log_path(log_dir, "webui-visitors", time.gmtime())
    stored = append_json_record(path, record)
    if stored:
        label = payload.path if payload.kind == "page-view" else payload.name
        log.info(
            "visitor %s %s %s %s (details: %s)",
            event_id,
            payload.kind,
            record["client_ip"],
            label,
            path,
        )
    else:
        log.warning("visitor %s not stored: daily log cap reached at %s", event_id, path)
    return ClientEventOut(event_id=event_id, stored=stored)
