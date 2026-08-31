"""Simulated USB volume injection (test/dev surface, never production).

POST /api/v1/usb/volumes - inject or update a bounded simulated volume.

create_app mounts this router ONLY when MDT_USB_SIMULATION=1, so a
production app answers 405 here and its OpenAPI contract never advertises
the POST. Simulated state lives in usb_volumes (the read side merges it
into listings only while the gate is on); this module owns the write side:
namespace enforcement, real-id collision refusal, and the capacity bound.
"""

from __future__ import annotations

import os
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, field_validator

from . import usb_volumes
from .usb_volumes import (
    _FAKES_LOCK,
    UsbVolume,
    UsbVolumesOut,
    VolumeKind,
    VolumeRole,
    _someone_watching,
    _to_out,
    _touch_client,
    _with_simulations,
    hide_reason_for,
)

router = APIRouter(prefix="/usb", tags=["usb"])

_MAX_SIMULATED_VOLUMES = 16
_SIMULATED_ID_PREFIX = "sim:"
_SIMULATION_ENV = "MDT_USB_SIMULATION"


def simulation_enabled() -> bool:
    """Explicit opt-in gate for the simulated-volume surface.

    Unset / "0" = disabled (production default). "1" = enabled (tests / dev
    without a stick). Anything else fails fast: a typo must never silently
    disable or enable a test-only surface.
    """
    raw = os.environ.get(_SIMULATION_ENV, "0")
    if raw in ("", "0"):
        return False
    if raw == "1":
        return True
    raise ValueError(f"{_SIMULATION_ENV} must be unset, '0' or '1'; got {raw!r}")


class UsbVolumePost(BaseModel):
    """Simulate: inject a fake present volume (never touches disk)."""

    model_config = ConfigDict(frozen=True)

    id: str | None = None
    name: str = "FAKE USB"
    mount_path: str | None = "/Volumes/FAKE-USB"
    kind: VolumeKind = "music"
    present: bool = True
    role: VolumeRole = "usb_stick"
    protocol: str | None = "USB"

    @field_validator("id")
    @classmethod
    def _validate_simulated_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        suffix = value.removeprefix(_SIMULATED_ID_PREFIX)
        if (
            not value.startswith(_SIMULATED_ID_PREFIX)
            or not suffix.strip()
            or value != value.strip()
        ):
            raise ValueError(
                "simulated volume id must use the reserved 'sim:' namespace"
            )
        return value


@router.post("/volumes", response_model=UsbVolumesOut)
def post_usb_volume(body: UsbVolumePost) -> UsbVolumesOut:
    """Inject or update bounded simulated state without touching a stick."""
    _touch_client()
    generated_suffix = "-".join(body.name.strip().lower().split()) or "unnamed"
    vol_id = body.id or f"{_SIMULATED_ID_PREFIX}{generated_suffix}"
    reason = hide_reason_for(body.role, protocol=body.protocol, name=body.name)
    if any(volume.id == vol_id for volume in usb_volumes._cached):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "usb_simulation_id_collision",
                "reason": "real_volume_id_reserved",
            },
        )
    with _FAKES_LOCK:
        fakes = usb_volumes._fakes
        if vol_id not in fakes and len(fakes) >= _MAX_SIMULATED_VOLUMES:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "usb_simulation_capacity_exceeded",
                    "reason": "simulated_volume_limit_reached",
                },
            )
        fakes[vol_id] = UsbVolume(
            id=vol_id,
            name=body.name,
            mount_path=body.mount_path,
            kind=body.kind,
            present=body.present,
            simulated=True,
            role=body.role,
            protocol=body.protocol,
            hide_reason=reason,
        )
    vols = _with_simulations(usb_volumes._cached)
    return UsbVolumesOut(
        volumes=[_to_out(v) for v in vols],
        scanned_at=time.time(),
        watching=_someone_watching(),
    )
