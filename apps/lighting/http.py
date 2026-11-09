"""Outside surface of ``apps.lighting`` for delivery layers and the LIGHT-06 HTTP router.

Delivery layers such as ``apps.opendj_cli`` and ``apps.webui`` may import this module and the
contract, never ``apps.lighting.patch`` or ``apps.lighting.stage_feed`` directly. Network
monitor and stage live-mode names are re-exported here (same pattern as ``apps.autodj.http``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from apps.lighting.contract import CONTRACT_VERSION, LIGHTING_FLAG_ID
from apps.lighting.monitor import ARTNET_PORT, SACN_PORT, MonitorConfig, start_monitor
from apps.lighting.patch import PatchError, default_patch_path, load_store
from apps.lighting.runtime import (
    FEED_ENDED_CODE,
    SLOW_CONSUMER_CODE,
    DeckFeed,
    FeedFrame,
    LightingRuntime,
    ReplayFeed,
)
from apps.lighting.stage_feed import FrameFeed, parse_frame, parse_frame_message, stage_patch_body
from apps.lighting.stage_replay import replay_query

LIGHTING_DISABLED_MESSAGE = (
    "lighting output is off in this build (feature flag lighting.output is off; "
    "see GET /api/v1/flags)"
)
RUNTIME_STATE_ATTR = "lighting_runtime"


def lighting_refusal(app: Any) -> dict[str, str] | None:
    store = getattr(app.state, "feature_flags", None)
    if store is None or not store.enabled(LIGHTING_FLAG_ID):
        return {
            "error": "flag_disabled",
            "flag": LIGHTING_FLAG_ID,
            "message": LIGHTING_DISABLED_MESSAGE,
        }
    return None


def bind_lighting_runtime(app: Any, runtime: LightingRuntime | None = None) -> LightingRuntime:
    existing = getattr(app.state, RUNTIME_STATE_ATTR, None)
    if runtime is not None:
        setattr(app.state, RUNTIME_STATE_ATTR, runtime)
        return runtime
    if isinstance(existing, LightingRuntime):
        return existing
    bound = LightingRuntime()
    setattr(app.state, RUNTIME_STATE_ATTR, bound)
    return bound


class LightingDeckStatus(BaseModel):
    deck: str
    bpm: float | None = None
    playing: bool = False
    section: str | None = None


class LightingStatus(BaseModel):
    flag: str = Field(default=LIGHTING_FLAG_ID)
    enabled: bool = True
    contract: str = Field(default=CONTRACT_VERSION)
    feed_connected: bool = False
    decks: list[LightingDeckStatus] = Field(default_factory=list)
    weights: dict[str, float] = Field(default_factory=dict)
    subscribers: int = 0


@asynccontextmanager
async def _router_lifespan(app: Any) -> AsyncIterator[None]:
    runtime = bind_lighting_runtime(app)
    if lighting_refusal(app) is None:
        await runtime.start()
    try:
        yield
    finally:
        await runtime.stop()


router = APIRouter(prefix="/lighting", tags=["lighting"], lifespan=_router_lifespan)


@router.get(
    "/status",
    response_model=LightingStatus,
    responses={409: {"description": "lighting.output is off"}},
)
def api_lighting_status(request: Request) -> dict[str, Any]:
    refusal = lighting_refusal(request.app)
    if refusal is not None:
        raise HTTPException(status_code=409, detail=refusal)
    return bind_lighting_runtime(request.app).status()


async def _events_pump(websocket: WebSocket, sub: Any) -> None:
    try:
        while not sub.overflow.is_set():
            item = await sub.queue.get()
            if item is None:
                await websocket.close(code=sub.close_code, reason=sub.close_reason)
                return
            await websocket.send_text(json.dumps(item, separators=(",", ":")))
    except WebSocketDisconnect:
        return


@router.websocket("/events")
async def api_lighting_events(websocket: WebSocket) -> None:
    refusal = lighting_refusal(websocket.app)
    if refusal is not None:
        await websocket.accept()
        await websocket.send_json({"type": "refused", "status": 409, "detail": refusal})
        await websocket.close(code=1008, reason="lighting.output is off")
        return

    runtime = bind_lighting_runtime(websocket.app)
    await websocket.accept()
    sub = runtime.hub.attach()
    pump: asyncio.Task[None] | None = None
    try:
        pump = asyncio.create_task(_events_pump(websocket, sub))
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        return
    finally:
        if pump is not None:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump
        runtime.hub.detach(sub)


__all__ = [
    "ARTNET_PORT",
    "FEED_ENDED_CODE",
    "LIGHTING_DISABLED_MESSAGE",
    "RUNTIME_STATE_ATTR",
    "SACN_PORT",
    "SLOW_CONSUMER_CODE",
    "DeckFeed",
    "FeedFrame",
    "FrameFeed",
    "LightingRuntime",
    "MonitorConfig",
    "PatchError",
    "ReplayFeed",
    "bind_lighting_runtime",
    "default_patch_path",
    "lighting_refusal",
    "load_store",
    "parse_frame",
    "parse_frame_message",
    "replay_query",
    "router",
    "stage_patch_body",
    "start_monitor",
]
