"""Operating-system master output volume.

GET /api/v1/system/audio/volume  - read {volume, muted, backend}
PUT /api/v1/system/audio/volume  - set {volume}, answer the state read back

Prefix-less: mounted under the shared ``/api/v1`` prefix. A host whose
platform arm cannot run answers 503 ``system_volume_unavailable`` with the
reason; nothing reports a volume it did not read.
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from apps.shared import system_volume

router = APIRouter(tags=["system"])
MOUNT_UNDER_API_PREFIX = True


class SystemVolumeOut(BaseModel):
    volume: int = Field(ge=0, le=100, description="Master output volume, integer percent.")
    muted: bool
    backend: str = Field(description="Which platform arm answered: osascript, wpctl, pactl, core_audio.")


class SystemVolumeIn(BaseModel):
    volume: int = Field(ge=0, le=100, strict=True)


class SystemVolumeErrorDetail(BaseModel):
    code: str
    reason: str


class SystemVolumeErrorOut(BaseModel):
    detail: SystemVolumeErrorDetail


_RESPONSES = {503: {"model": SystemVolumeErrorOut, "description": "This host cannot read or set its output volume."}}


def _unavailable(exc: system_volume.SystemVolumeUnavailable) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": {"code": exc.code, "reason": exc.reason}})


@router.get("/system/audio/volume", response_model=SystemVolumeOut, responses=_RESPONSES)
def get_system_volume() -> SystemVolumeOut | JSONResponse:
    try:
        return SystemVolumeOut(**asdict(system_volume.get_volume()))
    except system_volume.SystemVolumeUnavailable as exc:
        return _unavailable(exc)


@router.put("/system/audio/volume", response_model=SystemVolumeOut, responses=_RESPONSES)
def put_system_volume(body: SystemVolumeIn) -> SystemVolumeOut | JSONResponse:
    try:
        return SystemVolumeOut(**asdict(system_volume.set_volume(body.volume)))
    except system_volume.SystemVolumeUnavailable as exc:
        return _unavailable(exc)
