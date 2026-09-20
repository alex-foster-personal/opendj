"""AUDIO-DEVICE-01: OS output-device delivery probe and switch-output (issue #923)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from ..shell_output_health import ShellOutputHealthClient, data_dir_from_state_db
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
    checked_at: str


class AudioSwitchOutputOut(BaseModel):
    cycled: bool
    from_device: str | None = Field(default=None, alias="from")
    via: str | None = None
    restored: str | None = None
    error: str | None = None

    model_config = {"populate_by_name": True}


def _client(request: Request) -> ShellOutputHealthClient:
    data_dir = data_dir_from_state_db(resolve_state_db_path(request))
    return ShellOutputHealthClient(data_dir=Path(data_dir))


@router.get("/output-health", response_model=AudioOutputHealthOut)
def get_output_health(
    client: ShellOutputHealthClient = Depends(_client),  # noqa: B008
) -> dict[str, Any]:
    return client.get_output_health()


@router.post("/switch-output", response_model=AudioSwitchOutputOut)
def post_switch_output(
    client: ShellOutputHealthClient = Depends(_client),  # noqa: B008
) -> dict[str, Any]:
    payload = client.post_switch_output()
    if payload.get("cycled") is not True:
        from fastapi import HTTPException

        raise HTTPException(status_code=503, detail=payload)
    return payload


__all__ = ["router"]
