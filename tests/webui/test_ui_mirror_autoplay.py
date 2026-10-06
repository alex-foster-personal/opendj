"""AGENT-20: AutoPlay state in the UI mirror, and the agent-side AutoPlay switch.

Bug #2c (Tue 6 Oct 2026, silver preview soak): no endpoint said whether AutoPlay
was on or would hand off. These drive the real state and commands routers; the
open page is played by the test (claim, then post the page's result).
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any, get_args

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apps.webui.server.routes import state as state_routes
from apps.webui.server.routes.commands import router as commands_router

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "apps" / "webui" / "frontend" / "src" / "lib" / "rb"

ARMED = {"autoplay_enabled": True, "autoplay_armed": True, "autoplay_disarm_reason": None}


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(state_routes.router, prefix="/api/v1")
    app.include_router(commands_router, prefix="/api/v1")
    return app


def _doc(**fields: Any) -> dict[str, Any]:
    return {"client_open": True, "client_id": "tab-a", "decks": {}, "mirror_schema": 2, **ARMED, **fields}


def _run(body: Any) -> None:
    async def run() -> None:
        async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
            await body(client)

    asyncio.run(run())


async def _put(client: AsyncClient, document: dict[str, Any]) -> int:
    response = await client.put(
        "/api/v1/state/ui-mirror", json=document, headers={"x-opendj-lease": document["client_id"]}
    )
    return response.status_code


@pytest.mark.requirement("AGENT-20")
def test_a_schema_2_mirror_round_trips_the_three_fields() -> None:
    """[if] a page publishes schema 2 with autoplay fields [then] GET returns them, [else stop]."""

    async def body(client: AsyncClient) -> None:
        for fields in (
            ARMED,
            {"autoplay_enabled": True, "autoplay_armed": False, "autoplay_disarm_reason": "no-deck-playing"},
            {"autoplay_enabled": False, "autoplay_armed": False, "autoplay_disarm_reason": "disabled"},
        ):
            assert await _put(client, _doc(**fields)) == 202
            mirror = (await client.get("/api/v1/state/ui-mirror")).json()
            assert {key: mirror[key] for key in state_routes.AUTOPLAY_KEYS} == fields

    _run(body)


@pytest.mark.requirement("AGENT-20")
@pytest.mark.parametrize(
    "fields",
    [
        {"autoplay_armed": None},
        {"autoplay_enabled": "true"},
        {"autoplay_armed": False},
        {"autoplay_disarm_reason": "no-deck-playing"},
        {"autoplay_enabled": False},
        {"autoplay_enabled": False, "autoplay_armed": False, "autoplay_disarm_reason": "no-deck-playing"},
        {"autoplay_armed": False, "autoplay_disarm_reason": "made-up-reason"},
        {"mirror_schema": 1},
        {"mirror_schema": True},
    ],
)
def test_a_schema_2_mirror_with_bad_autoplay_fields_is_refused(fields: dict[str, Any]) -> None:
    """[if] schema-2 autoplay fields are wrong or inconsistent [then] 422 and nothing stored, [else stop]."""

    async def body(client: AsyncClient) -> None:
        assert await _put(client, _doc(**fields)) == 422
        assert (await client.get("/api/v1/state/ui-mirror")).status_code == 409

    _run(body)


@pytest.mark.requirement("AGENT-20")
@pytest.mark.parametrize("missing", state_routes.AUTOPLAY_KEYS)
def test_a_schema_2_mirror_missing_a_field_is_refused(missing: str) -> None:
    """[if] a schema-2 page omits one autoplay field [then] 422, never a default, [else stop]."""
    document = _doc()
    del document[missing]

    async def body(client: AsyncClient) -> None:
        assert await _put(client, document) == 422

    _run(body)


@pytest.mark.requirement("AGENT-20")
def test_an_unversioned_page_is_accepted_without_inventing_fields(caplog: pytest.LogCaptureFixture) -> None:
    """[if] a pre-AGENT-20 page publishes [then] 202, fields absent, one warning, [else stop]."""
    legacy = {"client_open": True, "client_id": "tab-old", "decks": {}}

    async def body(client: AsyncClient) -> None:
        with caplog.at_level(logging.WARNING, logger=state_routes.__name__):
            assert await _put(client, legacy) == 202
            assert await _put(client, legacy) == 202
        mirror = (await client.get("/api/v1/state/ui-mirror")).json()
        assert not any(key in mirror for key in state_routes.AUTOPLAY_KEYS)
        warnings = [r for r in caplog.records if "before AGENT-20" in r.getMessage()]
        assert len(warnings) == 1, "warned once per client, not once per second"

    _run(body)


@pytest.mark.requirement("AGENT-20")
def test_autoplay_fields_without_a_schema_are_still_validated() -> None:
    """[if] an unversioned PUT carries a bad autoplay field [then] 422, [else stop]."""
    document = {"client_open": True, "client_id": "tab-a", "decks": {}, "autoplay_enabled": 1}

    async def body(client: AsyncClient) -> None:
        assert await _put(client, document) == 422

    _run(body)


async def _claim_and_complete(client: AsyncClient, changed: dict[str, Any]) -> dict[str, Any]:
    for _ in range(200):
        await asyncio.sleep(0.01)
        claimed = (await client.get("/api/v1/commands/next")).json()
        if claimed is not None:
            accepted = await client.post(
                f"/api/v1/commands/{claimed['id']}/result",
                json={"steps": [{"status": "succeeded"}], "mirror_delta": {"changed": changed}},
            )
            assert accepted.status_code == 202
            return claimed
    raise AssertionError("the open page was never offered the order")


@pytest.mark.requirement("AGENT-20")
def test_the_autoplay_command_reaches_the_page_and_its_mirror_follows() -> None:
    """[if] an agent posts autoplay on/off [then] the page gets it and the mirror shows it, [else stop]."""

    async def body(client: AsyncClient) -> None:
        assert await _put(client, _doc()) == 202
        for enabled, fields in (
            (False, {"autoplay_enabled": False, "autoplay_armed": False, "autoplay_disarm_reason": "disabled"}),
            (True, ARMED),
        ):
            order = asyncio.create_task(
                client.post("/api/v1/commands", json={"single": {"type": "autoplay", "enabled": enabled}})
            )
            claimed = await _claim_and_complete(client, {"ui": {"auto_play_enabled": enabled}})
            assert claimed["payload"] == {"type": "autoplay", "enabled": enabled}
            response = await order
            assert response.status_code == 200
            assert response.json()["mirror_delta"]["changed"]["ui"]["auto_play_enabled"] is enabled
            # The page republishes right after a command (agent-orders.ts).
            assert await _put(client, _doc(**fields)) == 202
            mirror = (await client.get("/api/v1/state/ui-mirror")).json()
            assert mirror["autoplay_enabled"] is enabled

    _run(body)


@pytest.mark.requirement("AGENT-20")
@pytest.mark.parametrize(
    "order",
    [
        {"single": {"type": "autoplay", "enabled": "true"}},
        {"single": {"type": "autoplay", "enabled": 1}},
        {"single": {"type": "autoplay"}},
        {"single": {"type": "autoplay", "enabled": True, "deck": 1}},
        {"sequence": [{"type": "play", "deck": 1}, {"type": "autoplay", "enabled": None}]},
        {"parallel": [{"type": "autoplay", "on": True}]},
    ],
)
def test_a_malformed_autoplay_command_is_refused_before_the_page_sees_it(order: dict[str, Any]) -> None:
    """[if] an autoplay command is malformed [then] 422 and no order is queued, [else stop]."""

    async def body(client: AsyncClient) -> None:
        assert await _put(client, _doc()) == 202
        # Bounded: an order that slips past validation waits for a page forever.
        response = await asyncio.wait_for(client.post("/api/v1/commands", json=order), timeout=5)
        assert response.status_code == 422
        assert (await client.get("/api/v1/commands/next")).json() is None

    _run(body)


def _ts_union(path: Path, name: str) -> set[str]:
    text = path.read_text(encoding="utf-8")
    match = re.search(rf"export type {name} =(.*?);", text, re.S)
    assert match is not None, f"{name} not found in {path}"
    return set(re.findall(r"'([a-z-]+)'", match.group(1)))


@pytest.mark.requirement("AGENT-20")
def test_the_server_reasons_are_exactly_the_typescript_ones() -> None:
    """[if] the TS disarm or stall reason unions drift from the server Literal [then] fail, [else stop]."""
    page = _ts_union(LIB / "autoplay-status.ts", "AutoPlayDisarmReason")
    stall = _ts_union(LIB / "autoplay-stall.ts", "AutoPlayStallReason")
    assert "disabled" in page and "missing-audio" in stall, "control: the reader sees both unions"
    assert set(get_args(state_routes.AutoPlayDisarmReason)) == page | stall
