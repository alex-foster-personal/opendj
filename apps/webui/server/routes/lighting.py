"""Lighting stage visualiser HTTP and websocket surface (LIGHT-10573, issue #9898).

CONTRACT this router owns (mounted under ``/api/v1``):

  GET    /lighting/patch   200 ``{version, name, fixtures: [{id, x, y, groups}]}``: active venue patch
         404 ``{"detail": {"code": "no_patch", ...}}``: no ``<data>/lighting/patch.json``
         422 ``{"detail": {"code": "patch_invalid", "errors": [...]}}``: file fails to parse
  POST   /lighting/frames  body: one frame message -> 202 ``{"seq": n, "subscribers": k}``
         422 ``{"detail": {"code": "frame_invalid", "message": ...}}``
  WS     /lighting/frames  accept; send each published frame as JSON (newest wins); quiet until publish
"""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from typing import Any

from fastapi import APIRouter, Body, Request, WebSocket
from fastapi.responses import JSONResponse
from starlette.websockets import WebSocketDisconnect

from apps.lighting.http import (
    FrameFeed,
    PatchError,
    default_patch_path,
    load_store,
    parse_frame,
    stage_patch_body,
)

router = APIRouter(prefix="/lighting", tags=["lighting"])
MOUNT_UNDER_API_PREFIX = True


def _feed(app: Any) -> FrameFeed:
    feed = getattr(app.state, "lighting_frame_feed", None)
    if feed is None:
        feed = FrameFeed()
        app.state.lighting_frame_feed = feed
    return feed


def _patch_path(request: Request) -> Any:
    return getattr(request.app.state, "lighting_patch_path", None) or default_patch_path()


@router.get("/patch", response_model=None)
async def get_lighting_patch(request: Request) -> dict[str, Any] | JSONResponse:
    path = _patch_path(request)
    try:
        store = await asyncio.to_thread(load_store, path)
    except FileNotFoundError:
        return JSONResponse(
            status_code=404,
            content={
                "detail": {
                    "code": "no_patch",
                    "message": f"no lighting patch file at {path.name}",
                }
            },
        )
    except PatchError as exc:
        return JSONResponse(
            status_code=422,
            content={"detail": {"code": "patch_invalid", "errors": list(exc.errors)}},
        )
    except (json.JSONDecodeError, ValueError) as exc:
        return JSONResponse(
            status_code=422,
            content={"detail": {"code": "patch_invalid", "errors": [str(exc)]}},
        )
    return stage_patch_body(store)


@router.post("/frames", status_code=202, response_model=None)
async def publish_lighting_frame(
    request: Request,
    body: dict[str, Any] = Body(...),  # noqa: B008  # FastAPI DI
) -> dict[str, int] | JSONResponse:
    try:
        frame = parse_frame(body)
    except (TypeError, ValueError) as exc:
        return JSONResponse(
            status_code=422,
            content={"detail": {"code": "frame_invalid", "message": str(exc)}},
        )
    feed = _feed(request.app)
    seq = feed.publish(frame)
    return {"seq": seq, "subscribers": feed.subscriber_count}


async def _drain_client(websocket: WebSocket) -> None:
    while True:
        await websocket.receive_text()


@router.websocket("/frames")
async def lighting_frames_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    feed = _feed(websocket.app)

    async def pump() -> None:
        async for frame in feed.frames():
            await websocket.send_text(json.dumps(frame))

    pump_task = asyncio.create_task(pump())
    try:
        await _drain_client(websocket)
    except WebSocketDisconnect:
        pass
    finally:
        pump_task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await pump_task
