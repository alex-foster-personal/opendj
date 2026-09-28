"""``/api/v1/audio-engine`` -- where the Rust audio engine is, and its switch.

``GET`` is the origin route of plan 20-02: while ``odj-audio`` runs it returns
the engine's loopback WebSocket URL and the token a client presents as
``?token=``. The renderer connects there directly. The token is readable only
by callers this daemon already trusts: CORS admits the app's own origins
(``app_wiring._configure_cors``), so another web page cannot read it, and
without it the engine's socket answers 401.

``POST .../start`` and ``POST .../stop`` let an agent or a dev tab bring the
engine up or down without a restart; the origin guard already refuses those
from foreign pages.
"""

from __future__ import annotations

from typing import Literal

from fastapi import FastAPI, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from apps.engine_core.audio_engine import AudioEngineError, AudioEngineSupervisor

AUDIO_ENGINE_PATH: str = "/api/v1/audio-engine"
SUPERVISOR_STATE_ATTR: str = "audio_engine"

_ERROR_STATUS: dict[str, int] = {
    "invalid": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "conflict": status.HTTP_409_CONFLICT,
    "unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
}


class AudioEngineOut(BaseModel):
    state: Literal[
        "off", "starting", "running", "restarting", "stopped", "failed", "unavailable"
    ]
    clock: str | None = None
    binary: str | None = None
    binary_source: str | None = None
    pid: int | None = None
    ws_url: str | None = None
    token: str | None = None
    protocol: int | None = None
    engine: str | None = None
    sample_rate: int | None = None
    generation: int
    restarts: int
    last_exit_code: int | None = None
    error: str | None = None
    stderr_tail: list[str]


class AudioEngineStartIn(BaseModel):
    clock: Literal["wall", "device"]


def add_audio_engine_routes(app: FastAPI, supervisor: AudioEngineSupervisor) -> None:
    setattr(app.state, SUPERVISOR_STATE_ATTR, supervisor)

    def refusal(exc: AudioEngineError) -> JSONResponse:
        return JSONResponse(
            status_code=_ERROR_STATUS.get(exc.code, status.HTTP_500_INTERNAL_SERVER_ERROR),
            content={
                "error": f"audio_engine_{exc.code}",
                "message": str(exc),
                "details": {"status": supervisor.status()},
            },
        )

    @app.get(
        AUDIO_ENGINE_PATH,
        response_model=AudioEngineOut,
        tags=["audio-engine"],
        name="audio_engine_status",
    )
    def audio_engine_status() -> AudioEngineOut:
        return AudioEngineOut.model_validate(supervisor.status())

    @app.post(
        f"{AUDIO_ENGINE_PATH}/start",
        response_model=AudioEngineOut,
        tags=["audio-engine"],
        name="audio_engine_start",
        responses={
            status.HTTP_409_CONFLICT: {"description": "running on another clock"},
            status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "no odj-audio binary"},
        },
    )
    def audio_engine_start(body: AudioEngineStartIn) -> AudioEngineOut | JSONResponse:
        try:
            return AudioEngineOut.model_validate(supervisor.start(body.clock))
        except AudioEngineError as exc:
            return refusal(exc)

    @app.post(
        f"{AUDIO_ENGINE_PATH}/stop",
        response_model=AudioEngineOut,
        tags=["audio-engine"],
        name="audio_engine_stop",
    )
    def audio_engine_stop() -> AudioEngineOut:
        return AudioEngineOut.model_validate(supervisor.stop())


__all__ = [
    "AUDIO_ENGINE_PATH",
    "SUPERVISOR_STATE_ATTR",
    "AudioEngineOut",
    "add_audio_engine_routes",
]
