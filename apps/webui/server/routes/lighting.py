"""Lighting stage visualiser frame ingest (LIGHT-10573, issue #9898).

CONTRACT this router owns (mounted under ``/api/v1``):

  POST   /lighting/frames  body: one frame message -> 202 ``{"seq": n, "subscribers": k}``
         422 ``{"detail": {"code": "frame_invalid", "message": ...}}``

``GET /lighting/patch`` and websocket ``/lighting/frames`` belong to the daemon router in
``apps.lighting.http`` (issue #14062); ingest publishes to the same shared ``FrameFeed``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from apps.lighting.http import frame_feed, parse_frame

router = APIRouter(prefix="/lighting", tags=["lighting"])
MOUNT_UNDER_API_PREFIX = True


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
    feed = frame_feed(request.app)
    seq = feed.publish(frame)
    return {"seq": seq, "subscribers": feed.subscriber_count}
