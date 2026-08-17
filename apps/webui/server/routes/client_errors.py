"""Bounded browser-error sink with full details in a separate daily log."""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional, Union

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, field_validator

router = APIRouter(prefix="/client-errors", tags=["client-errors"])
log = logging.getLogger(__name__)

DEFAULT_LOG_DIR = Path.home() / ".local/share/music-dj-tools/webui"
MAX_DAILY_LOG_BYTES = 10 * 1024 * 1024
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


def _log_path(log_dir: Path, now: time.struct_time) -> Path:
    day = time.strftime("%Y-%m-%d", now)
    return log_dir / f"webui-client-errors-{day}.log"


def _append_record(path: Path, record: dict[str, object]) -> bool:
    line = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        current_size = path.stat().st_size
    except FileNotFoundError:
        current_size = 0
    if current_size + len(line) > MAX_DAILY_LOG_BYTES:
        return False
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, line)
    finally:
        os.close(descriptor)
    return True


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
    path = _log_path(log_dir, time.localtime())
    stored = _append_record(path, record)
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
