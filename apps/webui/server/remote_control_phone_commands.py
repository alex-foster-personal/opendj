"""Bounded AGENT-03 forwards for the paired phone remote (issue #10448)."""

from __future__ import annotations

import time
from typing import Any

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from apps.webui.server.remote_control_phone_requests import ENGINE_TIMEOUT_S, PhoneRequestRoutes

COMMAND_PATH = "/remote/api/command"
COMMAND_RATE_LIMIT = 20
COMMAND_RATE_WINDOW_S = 1.0


def validate_remote_command(body: object) -> dict[str, Any]:
    """Refuse any command shape outside the phone remote allowlist."""
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="body must be a JSON object")
    single = body.get("single")
    if not isinstance(single, dict):
        raise HTTPException(status_code=422, detail="only single commands are allowed")
    ctype = single.get("type")
    if ctype == "load":
        deck = single.get("deck")
        stable_id = single.get("stable_id")
        if not isinstance(deck, int) or not (1 <= deck <= 4):
            raise HTTPException(status_code=422, detail="load requires deck 1..4")
        if not isinstance(stable_id, str) or not stable_id.strip():
            raise HTTPException(status_code=422, detail="load requires stable_id")
        return {"single": {"type": "load", "deck": deck, "stable_id": stable_id.strip()}}
    if ctype == "play":
        deck = single.get("deck")
        playing = single.get("playing")
        if not isinstance(deck, int) or not (1 <= deck <= 4):
            raise HTTPException(status_code=422, detail="play requires deck 1..4")
        if not isinstance(playing, bool):
            raise HTTPException(status_code=422, detail="play requires playing bool")
        return {"single": {"type": "play", "deck": deck, "playing": playing}}
    raise HTTPException(status_code=422, detail="command type not allowed on the phone remote")


def take_command_token(session: Any, device_id: str) -> bool:
    """Return whether this device may forward another command in the current window."""
    now = time.monotonic()
    window = session._command_windows.setdefault(device_id, [now, 0])
    window_start = float(window[0])
    count = int(window[1])
    if now - window_start >= COMMAND_RATE_WINDOW_S:
        window_start, count = now, 0
    if count >= COMMAND_RATE_LIMIT:
        session._command_windows[device_id] = [window_start, count]
        return False
    session._command_windows[device_id] = [window_start, count + 1]
    return True


async def post_remote_command(api: PhoneRequestRoutes, request: Request) -> JSONResponse:
    record = api.session.authenticate(api.bearer_token(request), touch_last_seen=False)
    if not take_command_token(api.session, record.device_id):
        return JSONResponse(status_code=429, content={"detail": "rate_limited"})
    try:
        body = await request.json()
    except ValueError:
        body = None
    forward = validate_remote_command(body)
    client = api.session._command_http
    if client is None:
        client = httpx.AsyncClient(
            trust_env=False,
            timeout=ENGINE_TIMEOUT_S,
        )
        api.session._command_http = client
    try:
        response = await client.post(f"{api.base}/api/v1/commands", json=forward)
    except httpx.HTTPError:
        return JSONResponse(
            status_code=503,
            content={"error": "engine_unavailable", "message": "engine unavailable"},
        )
    try:
        payload = response.json()
    except ValueError:
        payload = {"error": "engine_unavailable", "message": "engine returned a non-JSON body"}
    return JSONResponse(status_code=response.status_code, content=payload)


def mount_phone_command_routes(app: Any, session: Any, engine_url: str) -> None:
    from apps.webui.server.remote_control import _bearer_token

    api = PhoneRequestRoutes(session, engine_url, _bearer_token)

    async def post_command(request: Request) -> JSONResponse:
        return await post_remote_command(api, request)

    app.add_api_route(COMMAND_PATH, post_command, methods=["POST"], response_model=None)


__all__ = ["COMMAND_PATH", "mount_phone_command_routes", "validate_remote_command"]
