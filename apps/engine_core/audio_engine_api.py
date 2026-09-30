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

import json
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from apps.engine_core.audio_engine import AudioEngineError, AudioEngineSupervisor
from apps.engine_core.audio_engine_load import EngineGrid, grid_from_anlz, load_command
from apps.shared import runtime_policy
from apps.webui.server import rb_vendor
from apps.webui.server.backend import StateBackend
from apps.webui.server.deps import get_read_state
from apps.webui.server.routes.rb_assets import get_track_anlz

AUDIO_ENGINE_PATH: str = "/api/v1/audio-engine"
SUPERVISOR_STATE_ATTR: str = "audio_engine"

_ERROR_STATUS: dict[str, int] = {
    "invalid": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "conflict": status.HTTP_409_CONFLICT,
    "unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
    "not_running": status.HTTP_409_CONFLICT,
    "timeout": status.HTTP_504_GATEWAY_TIMEOUT,
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


class AudioEngineLoadIn(BaseModel):
    deck: int = Field(ge=1, le=4)
    stable_id: str = Field(min_length=1)


class AudioEngineLoadOut(BaseModel):
    deck: int
    stable_id: str
    path: str
    beatgrid_source: str | None
    beats: int
    bpm: float | None
    #: Why the engine got no grid, when it got none.
    beatgrid_missing: str | None


def add_audio_engine_routes(app: FastAPI, supervisor: AudioEngineSupervisor) -> None:
    setattr(app.state, SUPERVISOR_STATE_ATTR, supervisor)

    def refusal(exc: AudioEngineError) -> JSONResponse:
        # Codes the engine itself answers (decode, io, invalid...) are a
        # refusal of this request, not a server fault.
        return JSONResponse(
            status_code=_ERROR_STATUS.get(exc.code, status.HTTP_422_UNPROCESSABLE_CONTENT),
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
        f"{AUDIO_ENGINE_PATH}/load",
        response_model=AudioEngineLoadOut,
        tags=["audio-engine"],
        name="audio_engine_load",
        responses={
            status.HTTP_409_CONFLICT: {"description": "the engine is not running"},
            status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "the engine refused the load"},
            status.HTTP_504_GATEWAY_TIMEOUT: {"description": "the engine did not answer"},
        },
    )
    def audio_engine_load(
        body: AudioEngineLoadIn,
        request: Request,
        backend: Annotated[StateBackend, Depends(get_read_state)],
    ) -> AudioEngineLoadOut | JSONResponse:
        """Load a library track onto a deck by ``stable_id``.

        The file is the one ``GET /tracks/{id}/audio`` would stream and the
        grid is the one ``GET /tracks/{id}/anlz`` serves, so the engine plays
        what the page shows. Returns once the engine has decoded the track.
        """
        picked = rb_vendor.resolve_playable_audio(
            body.stable_id,
            share=False,
            jobs_store=getattr(request.app.state, "jobs_store", None),
        )
        try:
            anlz_resp = get_track_anlz(
                request, body.stable_id, runtime_policy.ANLZ_POINTS_MIN, backend
            )
            anlz = json.loads(bytes(anlz_resp.body))
            grid = grid_from_anlz(anlz.get("beatgrid"))
        except HTTPException as exc:
            grid = EngineGrid([], None, None, f"anlz answered {exc.status_code}: {exc.detail}")
        except ValueError as exc:
            grid = EngineGrid([], None, None, str(exc))
        try:
            supervisor.command(load_command(body.deck, str(picked.path), grid))
        except AudioEngineError as exc:
            return refusal(exc)
        return AudioEngineLoadOut(
            deck=body.deck,
            stable_id=body.stable_id,
            path=str(picked.path),
            beatgrid_source=grid.source,
            beats=len(grid.beats),
            bpm=grid.bpm,
            beatgrid_missing=grid.missing_reason,
        )

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
