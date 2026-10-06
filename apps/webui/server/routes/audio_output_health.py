"""AUDIO-DEVICE-01: OS output-device delivery probe and switch-output (issue #923)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..shell_output_health import (
    SHELL_HEALTH_TIMEOUT_REASON,
    ShellHealthTimeout,
    ShellOutputHealthClient,
    data_dir_from_state_db,
)
from ..state_paths import resolve_state_db_path

router = APIRouter(prefix="/audio", tags=["audio"])


class AudioOutputHealthOut(BaseModel):
    device_delivering: bool | None
    verdict: str
    reason: str | None = None
    default_device_name: str | None = None
    default_device_uid: str | None = None
    io_cycles_advanced: bool | None = None
    hal_overload_recent: bool | None = None
    probe_available: bool
    # True when the default output is the MacBook speakers muted by an
    # occupied headphone jack (verdict ``muted_by_jack``).
    default_muted_by_jack: bool | None = None
    # The shell's current MASTER pin fault (kind, uid, stage, message), or null.
    master_pin_fault: dict[str, Any] | None = None
    checked_at: str


class AudioSwitchOutputOut(BaseModel):
    cycled: bool
    from_device: str | None = Field(default=None, alias="from")
    via: str | None = None
    restored: str | None = None
    error: str | None = None

    model_config = {"populate_by_name": True}


def _timeout_503(error: ShellHealthTimeout) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={"reason": SHELL_HEALTH_TIMEOUT_REASON, "verdict": "unknown", "message": str(error)},
    )


def _client(request: Request) -> ShellOutputHealthClient:
    data_dir = data_dir_from_state_db(resolve_state_db_path(request))
    return ShellOutputHealthClient(data_dir=Path(data_dir))


@router.get("/output-health", response_model=AudioOutputHealthOut)
def get_output_health(
    client: ShellOutputHealthClient = Depends(_client),  # noqa: B008
) -> dict[str, Any]:
    try:
        return client.get_output_health()
    except ShellHealthTimeout as error:
        raise _timeout_503(error) from error


@router.post("/switch-output", response_model=AudioSwitchOutputOut)
def post_switch_output(
    client: ShellOutputHealthClient = Depends(_client),  # noqa: B008
) -> dict[str, Any]:
    try:
        payload = client.post_switch_output()
    except ShellHealthTimeout as error:
        raise _timeout_503(error) from error
    if payload.get("cycled") is not True:
        raise HTTPException(status_code=503, detail=payload)
    return payload


__all__ = ["router"]
