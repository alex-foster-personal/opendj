"""Agent-readable UI mirror state.

The performance page owns the audio engine, so it pushes its rendered state to
this small engine-side store. A missing push is a closed page, never an empty
mirror that could be mistaken for an idle four-deck screen.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/state", tags=["agent-state"])


def _mirror_store(request: Request) -> dict[str, Any] | None:
    mirror = getattr(request.app.state, "ui_mirror", None)
    if mirror is None:
        return None
    if not isinstance(mirror, dict):
        raise TypeError("app.state.ui_mirror must be a JSON object")
    return mirror


@router.put("/ui-mirror", status_code=202)
async def publish_ui_mirror(request: Request, body: dict[str, Any]) -> dict[str, bool]:
    """Replace the page's current screen document with strict JSON input."""
    request.app.state.ui_mirror = deepcopy(body)
    return {"accepted": True}


@router.delete("/ui-mirror", status_code=204)
async def close_ui_mirror(request: Request) -> None:
    """Mark the performance page closed during its unmount lifecycle."""
    request.app.state.ui_mirror = None


@router.get("/ui-mirror", response_model=None)
async def get_ui_mirror(request: Request) -> dict[str, Any] | JSONResponse:
    """Return the latest page mirror, or fail explicitly when no page is open."""
    mirror = _mirror_store(request)
    if mirror is None:
        return JSONResponse(status_code=409, content={"client_open": False})
    return deepcopy(mirror)
