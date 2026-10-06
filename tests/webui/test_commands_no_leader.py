"""AGENT-21: POST /commands never hangs; an order no leader claims answers 503 no_leader.
[if] no page claims an order within the claim deadline [then] POST answers 503, [else stop].

Tue 6 Oct 2026, found by the #57 worker: a hidden tab that held its Web Lock but had
lost the mirror lease stopped claiming orders, the mirror still had a document on
record, so POST /commands passed its open-page check and then waited forever.
"""
from __future__ import annotations

import asyncio
import time

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apps.webui.server.routes.commands import (
    NO_LEADER_REASON,
    ORDER_CLAIM_DEADLINE_S,
    router,
)
from apps.webui.server.routes.state import router as state_router

pytestmark = pytest.mark.requirement("AGENT-21")

PLAY = {"single": {"type": "play", "deck": 1, "playing": True}}
DEADLINE_S = 0.5


def _app(deadline_s: float | None = DEADLINE_S) -> FastAPI:
    app = FastAPI()
    app.include_router(state_router, prefix="/api/v1")
    app.include_router(router, prefix="/api/v1")
    if deadline_s is not None:
        app.state.agent_order_claim_deadline_s = deadline_s
    return app


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _open_page(client: AsyncClient) -> None:
    published = await client.put("/api/v1/state/ui-mirror", json={"client_open": True})
    assert published.status_code == 202


def test_an_unclaimed_order_answers_503_no_leader_within_the_deadline() -> None:
    """[if] no page claims an order for 0.5 s [then] POST answers 503 no_leader, [else stop]."""

    async def run() -> None:
        async with _client(_app()) as client:
            await _open_page(client)
            started = time.monotonic()
            answer = await asyncio.wait_for(client.post("/api/v1/commands", json=PLAY), 5.0)
            elapsed = time.monotonic() - started
            assert answer.status_code == 503
            assert answer.json()["reason"] == NO_LEADER_REASON
            assert answer.json()["claim_deadline_s"] == DEADLINE_S
            assert DEADLINE_S <= elapsed < DEADLINE_S + 1.0, f"answered after {elapsed:.2f}s"
            late = await client.get("/api/v1/commands/next")
            assert late.status_code == 200
            assert late.json() is None, "a withdrawn order must not be claimed later"

    asyncio.run(run())


def test_a_claimed_order_waits_for_its_result_past_the_deadline() -> None:
    """[if] a page claims in time, answers late [then] POST returns its result, [else stop]."""

    async def run() -> None:
        async with _client(_app()) as client:
            await _open_page(client)
            order = asyncio.create_task(client.post("/api/v1/commands", json=PLAY))
            await asyncio.sleep(0.1)
            claimed = await client.get("/api/v1/commands/next")
            assert claimed.status_code == 200
            await asyncio.sleep(DEADLINE_S + 0.3)
            assert not order.done(), "a claimed order is not withdrawn by the claim deadline"
            done = await client.post(
                f"/api/v1/commands/{claimed.json()['id']}/result",
                json={"steps": [{"status": "succeeded"}], "mirror_delta": {"changed": {}}},
            )
            assert done.status_code == 202
            assert (await order).status_code == 200

    asyncio.run(run())


def test_the_claim_deadline_defaults_to_the_named_constant() -> None:
    """[if] the app sets no deadline [then] the documented 15 s constant applies, [else stop]."""
    assert ORDER_CLAIM_DEADLINE_S == 15.0


def test_a_bad_configured_deadline_fails_loudly() -> None:
    """[if] the deadline is not a positive number [then] POST raises TypeError, [else stop]."""

    async def run() -> None:
        async with _client(_app(deadline_s=0)) as client:
            await _open_page(client)
            with pytest.raises(TypeError, match="agent_order_claim_deadline_s"):
                await client.post("/api/v1/commands", json=PLAY)

    asyncio.run(run())
