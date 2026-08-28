"""Privacy-bounded page-view sink for local and remotely served web UIs."""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, field_validator

from ..client_logs import DEFAULT_LOG_DIR, append_json_record, daily_log_path

router = APIRouter(prefix="/client-events", tags=["client-events"])
log = logging.getLogger(__name__)


class ClientEventIn(BaseModel):
    client_event_id: str = Field(min_length=1, max_length=128)
    kind: Literal["page-view"]
    url: str = Field(min_length=1, max_length=4096)
    path: str = Field(min_length=1, max_length=2048)
    referrer: Optional[str] = Field(default=None, max_length=4096)
    client_timestamp: str = Field(max_length=128)
    user_agent: str = Field(max_length=2048)
    language: Optional[str] = Field(default=None, max_length=128)
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
    def urls_exclude_secrets(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("URL must be an absolute HTTP(S) URL")
        if parsed.query or parsed.fragment:
            raise ValueError("visitor URLs must exclude query and fragment")
        return value


class ClientEventOut(BaseModel):
    event_id: str
    stored: bool


@router.post("", response_model=ClientEventOut, status_code=202)
def capture_client_event(payload: ClientEventIn, request: Request) -> ClientEventOut:
    event_id = uuid.uuid4().hex[:16]
    received_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
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
    path = daily_log_path(log_dir, "webui-visitors", time.localtime())
    stored = append_json_record(path, record)
    if stored:
        log.info(
            "visitor %s %s %s %s (details: %s)",
            event_id,
            payload.kind,
            record["client_ip"],
            payload.path,
            path,
        )
    else:
        log.warning("visitor %s not stored: daily log cap reached at %s", event_id, path)
    return ClientEventOut(event_id=event_id, stored=stored)
