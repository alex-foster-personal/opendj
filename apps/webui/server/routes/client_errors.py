"""Bounded browser-error sink with full details in a separate daily log."""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional, Union

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, field_validator

from ..client_logs import DEFAULT_LOG_DIR, append_json_record, daily_log_path

router = APIRouter(prefix="/client-errors", tags=["client-errors"])
log = logging.getLogger(__name__)

ContextValue = Union[str, int, float, bool, None]


class ClientErrorIn(BaseModel):
    client_event_id: str = Field(min_length=1, max_length=128)
    kind: Literal["window-error", "unhandled-rejection", "sveltekit", "ui-error"]
    message: str = Field(min_length=1, max_length=4096)
    name: Optional[str] = Field(default=None, max_length=256)
    stack: Optional[str] = Field(default=None, max_length=32768)
    url: str = Field(max_length=4096)
    client_timestamp: str = Field(max_length=128)
    user_agent: str = Field(max_length=2048)
    secure_context: bool
    audio_worklet_available: bool
    context: dict[str, ContextValue] = Field(default_factory=dict, max_length=32)

    @field_validator("context")
    @classmethod
    def validate_context(
        cls, value: dict[str, ContextValue]
    ) -> dict[str, ContextValue]:
        for key, item in value.items():
            if len(key) > 128:
                raise ValueError("context keys must be at most 128 characters")
            if isinstance(item, str) and len(item) > 4096:
                raise ValueError("context strings must be at most 4096 characters")
        return value


class ClientErrorOut(BaseModel):
    event_id: str
    stored: bool


@router.post("", response_model=ClientErrorOut, status_code=202)
def capture_client_error(payload: ClientErrorIn, request: Request) -> ClientErrorOut:
    event_id = uuid.uuid4().hex[:16]
    received_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
    record = {
        "event_id": event_id,
        "received_at": received_at,
        "client_ip": request.client.host if request.client else None,
        **payload.model_dump(),
    }
    log_dir = Path(
        getattr(request.app.state, "client_error_log_dir", DEFAULT_LOG_DIR)
    )
    path = daily_log_path(log_dir, "webui-client-errors", time.localtime())
    stored = append_json_record(path, record)
    summary = payload.message.replace("\n", " ")[:300]
    if stored:
        log.error(
            "browser error %s %s: %s (details: %s)",
            event_id,
            payload.kind,
            summary,
            path,
        )
    else:
        log.error(
            "browser error %s not stored: daily log cap reached at %s (%s)",
            event_id,
            path,
            summary,
        )
    return ClientErrorOut(event_id=event_id, stored=stored)
