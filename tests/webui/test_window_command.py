"""Window command validation on POST /api/v1/commands and the UI-mirror windows field."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apps.webui.server.routes import state as state_routes
from apps.webui.server.routes.commands import _check_window_commands
from apps.webui.server.routes.commands import router as commands_router

ARMED = {"autoplay_enabled": True, "autoplay_armed": True, "autoplay_disarm_reason": None}
DOC = {"client_open": True, "client_id": "tab-a", "decks": {}, "mirror_schema": 2, **ARMED}


def _post(order: dict[str, Any]) -> tuple[int, Any]:
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

    asyncio.run(run())
    return out["status"], out["body"]


@pytest.mark.requirement("SECONDSCREEN-10757")
@pytest.mark.parametrize(
    "order, needle",
    [
        ({"single": {"type": "window", "action": "open", "label": "booth"}}, "unknown window label"),
        ({"single": {"type": "window", "action": "raise", "label": "second-screen"}}, "open, close or focus"),
        ({"single": {"type": "window", "action": "open"}}, "type, action and label"),
        (
            {"single": {"type": "window", "action": "open", "label": "second-screen", "extra": True}},
            "type, action and label",
        ),
    ],
)
def test_a_bad_window_command_is_a_422_before_the_page_sees_it(order: dict[str, Any], needle: str) -> None:
    """[if] the window command is malformed [then] 422 names the problem, [else stop]."""
    status, body = _post(order)
    assert status == 422
    assert needle in body["detail"]


@pytest.mark.requirement("SECONDSCREEN-10757")
def test_a_valid_window_command_passes_the_gate() -> None:
    """[if] action and label are known [then] validation accepts the command, [else stop]."""
    _check_window_commands({"single": {"type": "window", "action": "open", "label": "second-screen"}})
    _check_window_commands({"single": {"type": "window", "action": "close", "label": "second-screen"}})
    _check_window_commands({"single": {"type": "window", "action": "focus", "label": "second-screen"}})


@pytest.mark.requirement("SECONDSCREEN-10757")
def test_ui_mirror_get_defaults_windows_to_an_empty_list() -> None:
    """[if] the page has not published windows yet [then] GET still reports [], [else stop]."""
    app = FastAPI()
    app.include_router(state_routes.router, prefix="/api/v1")

    async def run() -> dict[str, Any]:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            put = await client.put("/api/v1/state/ui-mirror", json=DOC, headers={"x-opendj-lease": "tab-a"})
            assert put.status_code == 202
            response = await client.get("/api/v1/state/ui-mirror")
            assert response.status_code == 200
            return response.json()

    body = asyncio.run(run())
    assert body["windows"] == []
