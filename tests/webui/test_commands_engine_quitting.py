"""INSTALL-36: an agent order still waiting when the engine quits gets a terminal answer.
[if] the broker closes with an order pending [then] it answers 503 at once, [else stop].

Codex on #5844 (Wed 7 Oct 2026): closing the order broker released the held
long polls, but a poll released that way, or any poll arriving mid-teardown,
still CLAIMED a pending order. The page could never post its result back, so
the agent's POST /commands waited until uvicorn cancelled it, the same
shutdown-time failure the close exists to prevent.

Regression lines:
  - if a closed broker still hands out orders then a released poll claims one
    the page can never answer -> broken
  - (a pending order answering 503 on a real quit is proved on the real
    engine, tests/engine_core/test_quit_is_bounded.py)
  - if an open broker stops handing out orders then AGENT-19 is gone (the
    control) -> broken
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apps.webui.server.routes.commands import (
    close_order_broker,
    router,
)
from apps.webui.server.routes.state import router as state_router

pytestmark = pytest.mark.requirement("INSTALL-36")

PLAY = {"single": {"type": "play", "deck": 1, "playing": True}}


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(state_router, prefix="/api/v1")
    app.include_router(router, prefix="/api/v1")
    # Long enough that only the close, never the claim deadline, ends the POST.
    app.state.agent_order_claim_deadline_s = 30.0
    return app


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _open_page(client: AsyncClient) -> None:
    published = await client.put("/api/v1/state/ui-mirror", json={"client_open": True})
    assert published.status_code == 202


async def _pending_post(client: AsyncClient) -> asyncio.Task:
    post = asyncio.create_task(client.post("/api/v1/commands", json=PLAY))
    await asyncio.sleep(0.2)
    assert not post.done(), "the order was answered before anything closed"
    return post


def test_a_closed_broker_hands_no_order_to_a_poll() -> None:
    async def run() -> None:
        app = _app()
        async with _client(app) as client:
            await _open_page(client)
            post = await _pending_post(client)
            close_order_broker(app)
            polled = await client.get("/api/v1/commands/next", params={"wait_ms": 5000})
            assert polled.status_code == 200
            assert polled.json() is None, f"a closed broker handed out {polled.json()}"
            await asyncio.wait_for(post, 2.0)

    asyncio.run(run())


def test_an_open_broker_still_hands_the_order_to_a_poll() -> None:
    """The control: without the close, the same poll claims the same order."""

    async def run() -> None:
        app = _app()
        async with _client(app) as client:
            await _open_page(client)
            post = await _pending_post(client)
            polled = await client.get("/api/v1/commands/next", params={"wait_ms": 5000})
            assert polled.status_code == 200
            order = polled.json()
            assert order is not None and order.get("id"), f"an open broker handed out {order}"
            close_order_broker(app)  # answer the claimed POST so the client can close
            await asyncio.wait_for(post, 2.0)

    asyncio.run(run())
