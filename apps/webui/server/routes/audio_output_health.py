"""AUDIO-DEVICE-01: OS output-device delivery probe and switch-output (issue #923).

AUDIO-DEVICE-03..05: the main-output link check fields, the webview's acoustic
proof folded into the verdict, the CoreAudio input listing for its mic guard,
and the Bluetooth speaker reconnect.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from ..acoustic_proof import ACOUSTIC_MIC_TRANSPORTS, AcousticProof, AcousticVerdict, overlay_acoustic_proof
from ..shell_output_health import (
    SHELL_HEALTH_TIMEOUT_REASON,
    ShellHealthTimeout,
    ShellOutputHealthClient,
    data_dir_from_state_db,
)
from ..state_paths import resolve_state_db_path

router = APIRouter(prefix="/audio", tags=["audio"])
MOUNT_UNDER_API_PREFIX = True


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
    # AUDIO-DEVICE-03: the main-output link check (shell reads).
    default_device_transport: str | None = None
    bluetooth_address: str | None = None
    device_is_alive: bool | None = None
    running_somewhere_before_probe: bool | None = None
    sample_clock_ratio: float | None = None
    shell_link_fault: str | None = None
    # AUDIO-DEVICE-04: the latest fresh acoustic proof, or null.
    acoustic_proof: dict[str, Any] | None = None
    checked_at: str


class AudioSwitchOutputOut(BaseModel):
    cycled: bool
    from_device: str | None = Field(default=None, alias="from")
    via: str | None = None
    restored: str | None = None
    error: str | None = None

    model_config = {"populate_by_name": True}


class AudioReconnectSpeakerOut(BaseModel):
    reconnected: bool
    device_name: str | None = None
    device_uid: str | None = None
    bluetooth_address: str | None = None
    elapsed_ms: int | None = None
    error: str | None = None


class AudioInputDeviceOut(BaseModel):
    uid: str
    name: str
    transport: str
    channels: int
    acoustic_proof_allowed: bool


class AudioInputDevicesOut(BaseModel):
    inputs: list[AudioInputDeviceOut]
    error: str | None = None


class AcousticProofIn(BaseModel):
    verdict: AcousticVerdict
    failed_windows: int = Field(ge=0)
    windows_required: int = Field(ge=1)
    peak_correlation: float | None = None
    mic_label: str = Field(min_length=1)
    mic_transport: str

    @field_validator("mic_transport")
    @classmethod
    def _wired_or_usb_only(cls, value: str) -> str:
        if value not in ACOUSTIC_MIC_TRANSPORTS:
            raise ValueError(
                f"mic_transport {value!r} refused: the acoustic proof only uses "
                f"{'|'.join(sorted(ACOUSTIC_MIC_TRANSPORTS))} inputs, never Bluetooth (HFP)"
            )
        return value


class AcousticProofOut(BaseModel):
    verdict: AcousticVerdict
    failed_windows: int
    windows_required: int
    peak_correlation: float | None
    mic_label: str
    mic_transport: str
    received_at: str
    age_s: float


def _timeout_503(error: ShellHealthTimeout) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={"reason": SHELL_HEALTH_TIMEOUT_REASON, "verdict": "unknown", "message": str(error)},
    )


def _client(request: Request) -> ShellOutputHealthClient:
    data_dir = data_dir_from_state_db(resolve_state_db_path(request))
    return ShellOutputHealthClient(data_dir=Path(data_dir))


def _stored_proof(request: Request) -> AcousticProof | None:
    proof = getattr(request.app.state, "acoustic_proof", None)
    if proof is not None and not isinstance(proof, AcousticProof):
        raise TypeError(f"app.state.acoustic_proof must be an AcousticProof, got {type(proof).__name__}")
    return proof


@router.get("/output-health", response_model=AudioOutputHealthOut)
def get_output_health(
    request: Request,
    client: ShellOutputHealthClient = Depends(_client),  # noqa: B008
) -> dict[str, Any]:
    try:
        shell = client.get_output_health()
    except ShellHealthTimeout as error:
        raise _timeout_503(error) from error
    return overlay_acoustic_proof(shell, _stored_proof(request), time.monotonic())


@router.post("/acoustic-proof", response_model=AcousticProofOut)
def post_acoustic_proof(body: AcousticProofIn, request: Request) -> dict[str, Any]:
    """AUDIO-DEVICE-04: the webview's latest mic-vs-master verdict."""
    now = time.monotonic()
    proof = AcousticProof(
        verdict=body.verdict,
        failed_windows=body.failed_windows,
        windows_required=body.windows_required,
        peak_correlation=body.peak_correlation,
        mic_label=body.mic_label,
        mic_transport=body.mic_transport,
        received_at=datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        received_monotonic_s=now,
    )
    request.app.state.acoustic_proof = proof
    return proof.as_json(now)


@router.get("/input-devices", response_model=AudioInputDevicesOut)
def get_input_devices(
    client: ShellOutputHealthClient = Depends(_client),  # noqa: B008
) -> dict[str, Any]:
    try:
        return client.get_input_devices()
    except ShellHealthTimeout as error:
        raise _timeout_503(error) from error


@router.post("/reconnect-speaker", response_model=AudioReconnectSpeakerOut)
def post_reconnect_speaker(
    client: ShellOutputHealthClient = Depends(_client),  # noqa: B008
) -> dict[str, Any]:
    """AUDIO-DEVICE-05: disconnect and reconnect the main Bluetooth output."""
    try:
        payload = client.post_reconnect_speaker()
    except ShellHealthTimeout as error:
        raise _timeout_503(error) from error
    if payload.get("reconnected") is not True:
        raise HTTPException(status_code=503, detail=payload)
    return payload


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
