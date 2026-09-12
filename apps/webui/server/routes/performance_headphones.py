"""HTTP parity for in-page headphone IPC controls (CUEOUT-04)."""

from __future__ import annotations

import math
from copy import deepcopy
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .commands import page_is_open, submit_single_command

router = APIRouter(prefix="/performance/headphones", tags=["performance-headphones"])

HEADPHONE_IPC_ROUTES: dict[str, tuple[str, str]] = {
    "headphone_mix": ("POST", "/api/v1/performance/headphones/mix"),
    "headphone_level": ("POST", "/api/v1/performance/headphones/level"),
    "channel_cue": ("POST", "/api/v1/performance/headphones/channel-cue"),
    "output_mode": ("POST", "/api/v1/performance/headphones/output-mode"),
    "head_delay_ms": ("POST", "/api/v1/performance/headphones/head-delay"),
    "headphone_outputs_refresh": ("POST", "/api/v1/performance/headphones/outputs/refresh"),
    "headphone_output_select": ("POST", "/api/v1/performance/headphones/outputs/select"),
}

_PAGE_REQUIRED = "performance page is not attached"


class HeadphoneOutputDeviceOut(BaseModel):
    id: str
    label: str


class HeadphoneStateOut(BaseModel):
    mix: float
    level: float
    head_delay_ms: float
    selected_output_device_id: str | None
    output_mode: str
    outputs: list[HeadphoneOutputDeviceOut]
    supported: bool
    active: bool
    error: str | None


def _require_page(request: Request) -> None:
    if not page_is_open(request):
        raise HTTPException(status_code=503, detail=_PAGE_REQUIRED)


def _headphones_from_mirror(request: Request) -> dict[str, Any]:
    mirror = getattr(request.app.state, "ui_mirror", None)
    if not isinstance(mirror, dict):
        raise HTTPException(status_code=503, detail=_PAGE_REQUIRED)
    mixer = mirror.get("mixer")
    if not isinstance(mixer, dict):
        raise HTTPException(status_code=503, detail=_PAGE_REQUIRED)
    headphones = mixer.get("headphones")
    if not isinstance(headphones, dict):
        raise HTTPException(status_code=503, detail=_PAGE_REQUIRED)
    return headphones


def _headphone_state_out(request: Request) -> HeadphoneStateOut:
    return HeadphoneStateOut.model_validate(deepcopy(_headphones_from_mirror(request)))


def _deep_merge(base: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _merge_mirror_delta(request: Request, result: dict[str, Any]) -> None:
    mirror_delta = result.get("mirror_delta")
    if not isinstance(mirror_delta, dict):
        return
    changed = mirror_delta.get("changed")
    if not isinstance(changed, dict):
        return
    mirror = getattr(request.app.state, "ui_mirror", None)
    if not isinstance(mirror, dict):
        return
    request.app.state.ui_mirror = _deep_merge(mirror, changed)


def _validate_unit(value: object) -> float:
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        raise HTTPException(status_code=400, detail="value must be a finite number")
    if value < 0 or value > 1:
        raise HTTPException(status_code=400, detail="value must be within 0..1")
    return float(value)


def _validate_deck(deck: object) -> int:
    if deck not in (1, 2, 3, 4):
        raise HTTPException(
            status_code=400,
            detail=f"deck must be one of 1, 2, 3, 4; got {deck}",
        )
    return deck


def _validate_boolean(name: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise HTTPException(status_code=400, detail=f"{name} must be boolean")
    return value


def _validate_output_mode(mode: object) -> str:
    if mode not in ("practice", "two_outputs", "split_cable"):
        raise HTTPException(
            status_code=400,
            detail=(
                f"headphone output_mode must be practice, two_outputs, or split_cable; "
                f"got {mode}"
            ),
        )
    return mode


def _validate_head_delay_ms(value: object) -> float:
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        raise HTTPException(
            status_code=400,
            detail=f"head delay must be a finite number within 0..500, got {value}",
        )
    if value < 0 or value > 500:
        raise HTTPException(
            status_code=400,
            detail=f"head delay must be a finite number within 0..500, got {value}",
        )
    return float(value)


def _validate_device_id(device_id: object) -> str:
    if not isinstance(device_id, str) or device_id.strip() == "":
        raise HTTPException(status_code=400, detail="device_id must be a non-empty string")
    return device_id


async def _submit_headphone_command(
    request: Request, command: dict[str, Any]
) -> HeadphoneStateOut:
    _require_page(request)
    result = await submit_single_command(request, command)
    steps = result.get("steps")
    if isinstance(steps, list) and steps:
        first = steps[0]
        if isinstance(first, dict) and first.get("status") == "failed":
            error = first.get("error")
            raise HTTPException(
                status_code=400,
                detail=str(error) if error is not None else "command failed",
            )
    _merge_mirror_delta(request, result)
    return _headphone_state_out(request)


@router.get("", response_model=HeadphoneStateOut)
async def get_headphones(request: Request) -> HeadphoneStateOut:
    """Return the live headphone state from the attached performance page."""
    _require_page(request)
    return _headphone_state_out(request)


@router.post("/mix", response_model=HeadphoneStateOut)
async def post_headphone_mix(request: Request, body: dict[str, Any]) -> HeadphoneStateOut:
    _require_page(request)
    value = _validate_unit(body.get("value"))
    return await _submit_headphone_command(
        request, {"type": "headphone_mix", "value": value}
    )


@router.post("/level", response_model=HeadphoneStateOut)
async def post_headphone_level(request: Request, body: dict[str, Any]) -> HeadphoneStateOut:
    _require_page(request)
    value = _validate_unit(body.get("value"))
    return await _submit_headphone_command(
        request, {"type": "headphone_level", "value": value}
    )


@router.post("/channel-cue", response_model=HeadphoneStateOut)
async def post_channel_cue(request: Request, body: dict[str, Any]) -> HeadphoneStateOut:
    _require_page(request)
    deck = _validate_deck(body.get("deck"))
    enabled = _validate_boolean("enabled", body.get("enabled"))
    return await _submit_headphone_command(
        request, {"type": "channel_cue", "deck": deck, "enabled": enabled}
    )


@router.post("/output-mode", response_model=HeadphoneStateOut)
async def post_output_mode(request: Request, body: dict[str, Any]) -> HeadphoneStateOut:
    _require_page(request)
    mode = _validate_output_mode(body.get("mode"))
    return await _submit_headphone_command(request, {"type": "output_mode", "mode": mode})


@router.post("/head-delay", response_model=HeadphoneStateOut)
async def post_head_delay_ms(request: Request, body: dict[str, Any]) -> HeadphoneStateOut:
    _require_page(request)
    value = _validate_head_delay_ms(body.get("value"))
    return await _submit_headphone_command(
        request, {"type": "head_delay_ms", "value": value}
    )


@router.post("/outputs/refresh", response_model=HeadphoneStateOut)
async def post_headphone_outputs_refresh(
    request: Request, body: dict[str, Any] | None = None
) -> HeadphoneStateOut:
    _require_page(request)
    return await _submit_headphone_command(
        request, {"type": "headphone_outputs_refresh"}
    )


@router.post("/outputs/select", response_model=HeadphoneStateOut)
async def post_headphone_output_select(
    request: Request, body: dict[str, Any]
) -> HeadphoneStateOut:
    _require_page(request)
    device_id = _validate_device_id(body.get("device_id"))
    return await _submit_headphone_command(
        request, {"type": "headphone_output_select", "device_id": device_id}
    )
