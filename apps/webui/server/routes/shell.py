"""Shell route navigation over engine HTTP poll (issue #2866, ADR-0048).

Agents POST a target route; only the installed desktop shell webview polls
``GET /shell/navigate/pending`` and calls SvelteKit ``goto``. Browser tabs
opened via ``ui_url`` must not consume these requests.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from apps.webui.server.shell_navigate import ShellNavigateStore, validate_shell_route

router = APIRouter(prefix="/shell", tags=["shell"])
log = logging.getLogger(__name__)


def _store(request: Request) -> ShellNavigateStore:
    store = getattr(request.app.state, "shell_navigate", None)
    if store is None:
        store = ShellNavigateStore()
        request.app.state.shell_navigate = store
    return store


@router.post("/navigate", status_code=202)
async def post_shell_navigate(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    route = body.get("route")
    if not isinstance(route, str):
        raise HTTPException(status_code=422, detail="route required")
    try:
        validated = validate_shell_route(route)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    pending = _store(request).set_pending(validated)
    # The desktop shell's toast (issue #2879) has no server-side counterpart
    # unless this line exists: agent-native parity requires the same event be
    # observable programmatically, not just shown on screen.
    log.info("shell navigate requested: route=%s id=%s", pending.route, pending.id)
    return {"accepted": True, "id": pending.id, "route": pending.route}


@router.get("/navigate/pending")
async def get_shell_navigate_pending(request: Request) -> dict[str, Any]:
    pending = _store(request).get_pending()
    if pending is None:
        return {"pending": None}
    return {"pending": {"id": pending.id, "route": pending.route}}


@router.post("/navigate/ack")
async def ack_shell_navigate(request: Request, body: dict[str, Any]) -> dict[str, bool]:
    navigate_id = body.get("id")
    if not isinstance(navigate_id, str):
        raise HTTPException(status_code=422, detail="id required")
    if not _store(request).ack(navigate_id):
        raise HTTPException(status_code=404, detail="no matching pending navigation")
    return {"acknowledged": True}
