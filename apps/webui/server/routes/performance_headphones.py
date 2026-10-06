"""HTTP parity for in-page headphone IPC controls (CUEOUT-04)."""

from __future__ import annotations

import math
from copy import deepcopy
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import BaseModel

from apps.webui.server.headphone_reports import client_id_of, headphone_reports

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
    "headphone_master_select": ("POST", "/api/v1/performance/headphones/outputs/master"),
    "headphone_input_select": ("POST", "/api/v1/performance/headphones/inputs/select"),
    # CUEOUT-14: cue/master alignment.
    "headphone_alignment_mode": ("POST", "/api/v1/performance/headphones/alignment-mode"),
    "master_delay_ms": ("POST", "/api/v1/performance/headphones/master-delay"),
    "headphone_calibrate": ("POST", "/api/v1/performance/headphones/calibrate"),
    "headphone_calibrate_abort": ("POST", "/api/v1/performance/headphones/calibrate/abort"),
    # CUEOUT-15: the library preview is a cue-bus voice, so it mirrors here
    # with the rest of the cue controls rather than beside the deck transport.
    "preview_cue": ("POST", "/api/v1/performance/headphones/preview"),
    "preview_stop": ("POST", "/api/v1/performance/headphones/preview/stop"),
}

HEADPHONE_ALIGNMENT_MODES = ("headphones_only", "delay_all", "hybrid")
# keep in sync with apps/webui/frontend/src/lib/player/constants.ts
MASTER_DELAY_MAX_MS = 1500

_PAGE_REQUIRED = "performance page is not attached"


class HeadphoneOutputDeviceOut(BaseModel):
    id: str
    label: str
    # CUEOUT-26, Mac shell only: the output that actually sounds for this one
    # (the occupied headphone jack for the muted MacBook speakers), whether the
    # jack has muted it, and its CoreAudio transport. Null in a browser listing.
    physical_id: str | None = None
    muted_by_jack: bool | None = None
    transport: str | None = None


class HeadphoneCalibrationProbeOut(BaseModel):
    """CUEOUT-14 stage one, live: the rung being tried and how close it is to heard.

    The ear-cup step is interactive, so this is the feedback an agent needs to
    drive it the way the operator does, watching `best` climb toward `threshold`.
    """

    bus: str
    gain: float
    peak: float | None
    lag_ms: float | None
    best: float
    threshold: float


class HeadphoneCalibrationOut(BaseModel):
    """CUEOUT-14 calibration progress; `step` is the modal's state machine."""

    step: str
    cue_latency_ms: float | None
    master_latency_ms: float | None
    offset_ms: float | None
    verify_residual_ms: float | None
    probe: HeadphoneCalibrationProbeOut | None
    error: str | None


class IoDeviceAccessOut(BaseModel):
    """IOPIN-14: whether the device lists could be read, and what to do if not.

    `status` is one of not_checked, listed, permission_needed, permission_denied,
    api_missing, enumeration_failed, timeout. Only `listed` means `outputs` and
    `inputs` are the machine's real device names; an agent must read this before
    treating a short list as a machine with few devices.
    """

    status: str
    action: str
    message: str | None
    detail: str | None
    output_pinning: bool
    notices: list[str]


class HeadphoneStateOut(BaseModel):
    mix: float
    level: float
    head_delay_ms: float
    alignment_mode: str
    master_delay_ms: float
    calibration: HeadphoneCalibrationOut
    selected_output_device_id: str | None
    selected_master_output_device_id: str | None
    selected_input_device_id: str | None
    output_mode: str
    # CUEOUT-26: "same_device" while split cue runs because MAIN and CUE are one
    # physical output (master L / cue R); null when the operator chose the mode.
    split_reason: str | None = None
    outputs: list[HeadphoneOutputDeviceOut]
    inputs: list[HeadphoneOutputDeviceOut]
    supported: bool
    active: bool
    error: str | None
    device_access: IoDeviceAccessOut
    # CUEOUT-18: which open page this state came from, and how old its report
    # is. Both null only when the state came from a mirror with no report.
    reporting_client_id: str | None = None
    report_age_ms: float | None = None


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
    """The state after a command: the mirror the command's delta was merged into."""
    return HeadphoneStateOut.model_validate(deepcopy(_headphones_from_mirror(request)))


def _best_headphone_state_out(request: Request) -> HeadphoneStateOut:
    """CUEOUT-18: the best-informed current client's state, not the last writer's."""
    reports = headphone_reports(request.app)
    report = reports.best()
    if report is None:
        return _headphone_state_out(request)
    return HeadphoneStateOut.model_validate(
        {
            **deepcopy(report.headphones),
            "reporting_client_id": report.client_id,
            "report_age_ms": round(reports.age_ms(report), 1),
        }
    )


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
    # The delta lands on the same client's report the mirror document came
    # from, so a read straight after a command sees it (CUEOUT-18).
    mixer = changed.get("mixer")
    headphones = mixer.get("headphones") if isinstance(mixer, dict) else None
    if isinstance(headphones, dict):
        headphone_reports(request.app).merge(client_id_of(mirror), headphones)


def _validate_unit(value: object) -> float:
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        raise HTTPException(status_code=400, detail="value must be a finite number")
    if value < 0 or value > 1:
        raise HTTPException(status_code=400, detail="value must be within 0..1")
    return float(value)


def _validate_optional_bpm(value: object) -> float | None:
    """Caller's own copy of the previewed track's BPM, for the R6 tempo match.

    Absent means "do not tempo-match", which is a different statement from a
    bad number, so a present-but-unusable value is a 400 rather than a silent
    drop back to the track's own tempo.
    """
    if value is None:
        return None
    if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise HTTPException(status_code=400, detail="bpm must be a positive finite number")
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
                f"headphone output_mode must be practice, two_outputs, or split_cable; got {mode}"
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


def _validate_alignment_mode(value: object) -> str:
    if value not in HEADPHONE_ALIGNMENT_MODES:
        raise HTTPException(
            status_code=400,
            detail=(
                "headphone alignment_mode must be headphones_only, delay_all, or hybrid; "
                f"got {value}"
            ),
        )
    return value


def _validate_master_delay_ms(value: object) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value < 0
        or value > MASTER_DELAY_MAX_MS
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"master delay must be a finite number within 0..{MASTER_DELAY_MAX_MS}, got {value}"
            ),
        )
    return float(value)


def _validate_stable_id(stable_id: object) -> str:
    if not isinstance(stable_id, str) or stable_id.strip() == "":
        raise HTTPException(status_code=400, detail="stable_id must be a non-empty string")
    return stable_id


def _validate_device_id(device_id: object) -> str:
    if not isinstance(device_id, str) or device_id.strip() == "":
        raise HTTPException(status_code=400, detail="device_id must be a non-empty string")
    return device_id


async def _submit_headphone_command(request: Request, command: dict[str, Any]) -> HeadphoneStateOut:
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
    """Return the live headphone state from the best-informed open performance page.

    With several pages open, a page whose device lists were read (`listed`) is
    not replaced by another page whose lists were not; `reporting_client_id`
    and `report_age_ms` say which page answered and how fresh it is.
    """
    _require_page(request)
    return _best_headphone_state_out(request)


@router.post(
    "/mix",
    response_model=HeadphoneStateOut,
    response_description="Headphone mix state after setting CUE-to-MASTER MIX",
)
async def post_headphone_mix(
    request: Request,
    body: Annotated[dict[str, Any], Body(..., title="HeadphoneMixBody")],
) -> HeadphoneStateOut:
    _require_page(request)
    value = _validate_unit(body.get("value"))
    return await _submit_headphone_command(request, {"type": "headphone_mix", "value": value})


@router.post(
    "/level",
    response_model=HeadphoneStateOut,
    response_description="Headphone state after setting monitor LEVEL",
)
async def post_headphone_level(
    request: Request,
    body: Annotated[dict[str, Any], Body(..., title="HeadphoneLevelBody")],
) -> HeadphoneStateOut:
    _require_page(request)
    value = _validate_unit(body.get("value"))
    return await _submit_headphone_command(request, {"type": "headphone_level", "value": value})


@router.post(
    "/channel-cue",
    response_model=HeadphoneStateOut,
    response_description="Headphone state after toggling a channel CUE",
)
async def post_channel_cue(
    request: Request,
    body: Annotated[dict[str, Any], Body(..., title="HeadphoneChannelCueBody")],
) -> HeadphoneStateOut:
    _require_page(request)
    deck = _validate_deck(body.get("deck"))
    enabled = _validate_boolean("enabled", body.get("enabled"))
    return await _submit_headphone_command(
        request, {"type": "channel_cue", "deck": deck, "enabled": enabled}
    )


@router.post(
    "/output-mode",
    response_model=HeadphoneStateOut,
    response_description="Headphone state after setting MAIN / two outputs / SPLIT",
)
async def post_output_mode(
    request: Request,
    body: Annotated[dict[str, Any], Body(..., title="HeadphoneOutputModeBody")],
) -> HeadphoneStateOut:
    _require_page(request)
    mode = _validate_output_mode(body.get("mode"))
    return await _submit_headphone_command(request, {"type": "output_mode", "mode": mode})


@router.post(
    "/head-delay",
    response_model=HeadphoneStateOut,
    response_description="Headphone state after setting Mixxx HEAD DELAY",
)
async def post_head_delay_ms(
    request: Request,
    body: Annotated[dict[str, Any], Body(..., title="HeadphoneHeadDelayBody")],
) -> HeadphoneStateOut:
    _require_page(request)
    value = _validate_head_delay_ms(body.get("value"))
    return await _submit_headphone_command(request, {"type": "head_delay_ms", "value": value})


@router.post(
    "/alignment-mode",
    response_model=HeadphoneStateOut,
    response_description="Headphone state after switching how the cue/master offset is split",
)
async def post_headphone_alignment_mode(
    request: Request,
    body: Annotated[dict[str, Any], Body(..., title="HeadphoneAlignmentModeBody")],
) -> HeadphoneStateOut:
    """CUEOUT-14: headphones_only, delay_all, or hybrid. Re-applies the last
    calibration without re-measuring."""
    _require_page(request)
    value = _validate_alignment_mode(body.get("value"))
    return await _submit_headphone_command(
        request, {"type": "headphone_alignment_mode", "value": value}
    )


@router.post(
    "/master-delay",
    response_model=HeadphoneStateOut,
    response_description="Headphone state after setting the room (MASTER) delay",
)
async def post_master_delay_ms(
    request: Request,
    body: Annotated[dict[str, Any], Body(..., title="HeadphoneMasterDelayBody")],
) -> HeadphoneStateOut:
    """CUEOUT-14: room delay line, 0..1500 ms, the last node before the output."""
    _require_page(request)
    value = _validate_master_delay_ms(body.get("value"))
    return await _submit_headphone_command(request, {"type": "master_delay_ms", "value": value})


@router.post(
    "/calibrate",
    response_model=HeadphoneStateOut,
    response_description="Headphone state after a headless cue alignment calibration",
)
async def post_headphone_calibrate(request: Request) -> HeadphoneStateOut:
    """CUEOUT-14: run the mic calibration headlessly (no ear-cup pause). The
    call holds until the run reaches applied (200) or failed (400 with the
    page's error); an abort resolves with the state at idle."""
    _require_page(request)
    return await _submit_headphone_command(request, {"type": "headphone_calibrate"})


@router.post(
    "/calibrate/abort",
    response_model=HeadphoneStateOut,
    response_description="Headphone state after aborting an in-flight calibration",
)
async def post_headphone_calibrate_abort(request: Request) -> HeadphoneStateOut:
    """CUEOUT-14: stop a running calibration; decks resume, nothing is applied."""
    _require_page(request)
    return await _submit_headphone_command(request, {"type": "headphone_calibrate_abort"})


@router.post(
    "/outputs/refresh",
    response_model=HeadphoneStateOut,
    response_description="Headphone state after re-enumerating outputs and inputs",
)
async def post_headphone_outputs_refresh(
    request: Request,
    _body: Annotated[dict[str, Any] | None, Body(title="HeadphoneOutputsRefreshBody")] = None,
) -> HeadphoneStateOut:
    _require_page(request)
    return await _submit_headphone_command(request, {"type": "headphone_outputs_refresh"})


@router.post(
    "/outputs/select",
    response_model=HeadphoneStateOut,
    response_description="Headphone state after selecting the HEADPHONE CUE sink",
)
async def post_headphone_output_select(
    request: Request,
    body: Annotated[dict[str, Any], Body(..., title="HeadphoneCueOutputBody")],
) -> HeadphoneStateOut:
    _require_page(request)
    device_id = _validate_device_id(body.get("device_id"))
    return await _submit_headphone_command(
        request, {"type": "headphone_output_select", "device_id": device_id}
    )


@router.post(
    "/outputs/master",
    response_model=HeadphoneStateOut,
    response_description="Headphone state after selecting the MASTER/MAIN sink",
)
async def post_headphone_master_select(
    request: Request,
    body: Annotated[dict[str, Any], Body(..., title="HeadphoneMasterOutputBody")],
) -> HeadphoneStateOut:
    _require_page(request)
    device_id = _validate_device_id(body.get("device_id"))
    return await _submit_headphone_command(
        request, {"type": "headphone_master_select", "device_id": device_id}
    )


@router.post(
    "/inputs/select",
    response_model=HeadphoneStateOut,
    response_description="Headphone state after selecting AUDIO IN",
)
async def post_headphone_input_select(
    request: Request,
    body: Annotated[dict[str, Any], Body(..., title="HeadphoneAudioInputBody")],
) -> HeadphoneStateOut:
    _require_page(request)
    device_id = _validate_device_id(body.get("device_id"))
    return await _submit_headphone_command(
        request, {"type": "headphone_input_select", "device_id": device_id}
    )


@router.post(
    "/preview",
    response_model=HeadphoneStateOut,
    response_description="Cue-bus state after starting the library preview",
)
async def post_preview_cue(
    request: Request,
    body: Annotated[dict[str, Any], Body(..., title="PreviewCueBody")],
) -> HeadphoneStateOut:
    """CUEOUT-15: play `stable_id` from `ratio` of its length on the cue bus.

    This is the mini-waveform click, driven without a pointer. It loads no
    deck and moves no transport, so it can be issued while all four decks
    are busy and while one of them is on air.

    A preview that could not be heard is a 400 carrying the reason the page
    would have shown the operator (no audio graph, a dead cue sink, MIX at
    the master end, GAIN at zero), never a 200 over silence.

    Optional `bpm` is the track's own tempo; with the preview tempo-match
    setting on and a master deck playing, the preview matches that tempo
    (CUEOUT-15 R6). Omit it and the preview plays at the track's own tempo.
    """
    _require_page(request)
    stable_id = _validate_stable_id(body.get("stable_id"))
    ratio = _validate_unit(body.get("ratio"))
    bpm = _validate_optional_bpm(body.get("bpm"))
    command: dict[str, Any] = {"type": "preview_cue", "stable_id": stable_id, "ratio": ratio}
    if bpm is not None:
        command["bpm"] = bpm
    return await _submit_headphone_command(request, command)


@router.post(
    "/preview/stop",
    response_model=HeadphoneStateOut,
    response_description="Cue-bus state after stopping the library preview",
)
async def post_preview_stop(
    request: Request,
    # Declared so the endpoint accepts and documents an explicit JSON body
    # even though stopping takes no fields; the sibling refresh route has the
    # same shape.
    body: Annotated[dict[str, Any] | None, Body(title="PreviewStopBody")] = None,  # noqa: ARG001
) -> HeadphoneStateOut:
    """CUEOUT-15: stop the preview and release the decoded track.

    Idempotent: stopping when nothing is previewing is a success, because
    the caller's intent (nothing playing on the preview voice) is satisfied.
    """
    _require_page(request)
    return await _submit_headphone_command(request, {"type": "preview_stop"})
