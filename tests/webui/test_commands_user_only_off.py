"""DECKUX-39 and PLAY-18: an agent command may switch Quantize or AutoPlay off only with ``by_user: true``.

A command without the flag is refused with a 422 that names the flag, so the
agent learns at once; app-internal paths revert on the page instead.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apps.webui.server.routes import state as state_routes
from apps.webui.server.routes.commands import _check_user_only_offs
from apps.webui.server.routes.commands import router as commands_router

ARMED = {"autoplay_enabled": True, "autoplay_armed": True, "autoplay_disarm_reason": None}
DOC = {"client_open": True, "client_id": "tab-a", "decks": {}, "mirror_schema": 2, **ARMED}


def _post(order: dict[str, Any]) -> tuple[int, Any, Any]:
    app = FastAPI()
    app.include_router(state_routes.router, prefix="/api/v1")
    app.include_router(commands_router, prefix="/api/v1")
    out: dict[str, Any] = {}

    async def run() -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            put = await client.put("/api/v1/state/ui-mirror", json=DOC, headers={"x-opendj-lease": "tab-a"})
            assert put.status_code == 202
            response = await asyncio.wait_for(client.post("/api/v1/commands", json=order), timeout=5)
            out["status"], out["body"] = response.status_code, response.json()
            out["queued"] = (await client.get("/api/v1/commands/next")).json()

    asyncio.run(run())
    return out["status"], out["body"], out["queued"]


@pytest.mark.requirement("DECKUX-39")
@pytest.mark.requirement("PLAY-18")
@pytest.mark.parametrize(
    "order",
    [
        {"single": {"type": "quantize", "deck": 1, "enabled": False}},
        {"single": {"type": "quantize", "deck": 1, "enabled": False, "by_user": False}},
        {"sequence": [{"type": "quantize", "deck": 2, "enabled": False}]},
        {"single": {"type": "autoplay", "enabled": False}},
        {"single": {"type": "autoplay", "enabled": False, "by_user": False}},
    ],
)
def test_a_user_only_off_without_by_user_is_a_422_naming_the_flag(order: dict[str, Any]) -> None:
    """[if] an agent sends quantize or autoplay off without by_user [then] 422 names by_user, [else stop]."""
    status, body, queued = _post(order)
    assert status == 422
    assert "send by_user: true when a person asked for this" in body["detail"]
    assert queued is None, "a refused order must never reach the page"


@pytest.mark.requirement("DECKUX-39")
def test_a_non_boolean_by_user_is_a_422() -> None:
    """[if] by_user is not a boolean [then] the order is a 422, [else stop]."""
    status, body, _ = _post({"single": {"type": "quantize", "deck": 1, "enabled": True, "by_user": "yes"}})
    assert status == 422
    assert "by_user must be boolean" in body["detail"]



@pytest.mark.requirement("PLAY-18")
@pytest.mark.parametrize(
    "order",
    [
        {"single": {"type": "autoplay", "enabled": False, "by_user": True}},
        {"single": {"type": "autoplay", "enabled": True}},
        {"single": {"type": "quantize", "deck": 1, "enabled": False, "by_user": True}},
        {"sequence": [{"type": "play", "deck": 1}, {"type": "quantize", "deck": 1, "enabled": True}]},
    ],
)
def test_a_user_off_or_any_on_passes_the_gate(order: dict[str, Any]) -> None:
    """[if] the off carries by_user or the command is an on [then] the gate lets it through, [else stop]."""
    _check_user_only_offs(order)
