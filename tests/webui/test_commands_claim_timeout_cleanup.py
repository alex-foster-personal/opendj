"""AGENT-21: a claimed order that times out leaves no held claim behind (issue #13211).
[if] a claimed order gets no result within the result deadline [then] nothing stays held, [else stop].

(private path), "Scope and diagnosis": a closed page can
claim an order and never answer. POST /commands answers 503 page_gone after the
result deadline; this file pins the cleanup after that answer and that the normal
result path is unchanged when the page answers in time.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from apps.webui.server.routes.commands import (
    PAGE_GONE_REASON,
    _app_broker,
    router,
    submit_single_command_to_app,
)
from apps.webui.server.routes.state import router as state_router

pytestmark = pytest.mark.requirement("AGENT-21")

PLAY = {"single": {"type": "play", "deck": 1, "playing": True}}
RESULT = {"steps": [{"status": "succeeded"}], "mirror_delta": {"changed": {}}}
CLAIM_DEADLINE_S = 2.0
RESULT_DEADLINE_S = 0.5


def _app() -> FastAPI:
    """Build an engine app with short claim and result deadlines."""
    app = FastAPI()
    app.include_router(state_router, prefix="/api/v1")
    app.include_router(router, prefix="/api/v1")
    app.state.agent_order_claim_deadline_s = CLAIM_DEADLINE_S
    app.state.agent_order_result_deadline_s = RESULT_DEADLINE_S
    return app


def _client(app: FastAPI) -> AsyncClient:
    """Return an in-process HTTP client for ``app``."""
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _open_page(client: AsyncClient) -> None:
    """Publish an open performance page so orders are accepted."""
    published = await client.put("/api/v1/state/ui-mirror", json={"client_open": True})
    assert published.status_code == 202


async def _claim(client: AsyncClient) -> dict[str, object]:
    """Claim the next order as the page would, waiting briefly for it to arrive."""
    claimed = await client.get("/api/v1/commands/next", params={"wait_ms": 2000})
    assert claimed.status_code == 200
    body = claimed.json()
    assert body is not None, "the posted order must be claimable"
    return body


def test_a_timed_out_claim_answers_page_gone_and_holds_nothing() -> None:
    """[if] a command claim reaches its timeout without a response [then] 503 page_gone and the broker holds no claim, [else stop]."""

    async def run() -> None:
        app = _app()
        async with _client(app) as client:
            await _open_page(client)
            started = time.monotonic()
            order = asyncio.create_task(client.post("/api/v1/commands", json=PLAY))
            await _claim(client)
            answer = await asyncio.wait_for(order, 5.0)
            elapsed = time.monotonic() - started
            assert answer.status_code == 503
            assert answer.json()["reason"] == PAGE_GONE_REASON
            assert elapsed < RESULT_DEADLINE_S + 1.5, f"answered after {elapsed:.2f}s"
            broker = _app_broker(app)
            assert broker.pending == {}, "the timed-out order must be discarded"
            assert broker.running() == 0, "no claim may stay held after the timeout"

    asyncio.run(run())


def test_polling_after_timeout_cleanup_finds_no_order_and_rejects_a_late_result() -> None:
    """[if] the same command is polled after timeout cleanup [then] next is null and a late result is 404, [else stop]."""

    async def run() -> None:
        async with _client(_app()) as client:
            await _open_page(client)
            order = asyncio.create_task(client.post("/api/v1/commands", json=PLAY))
            claimed = await _claim(client)
            answer = await asyncio.wait_for(order, 5.0)
            assert answer.status_code == 503
            again = await client.get("/api/v1/commands/next")
            assert again.status_code == 200
            assert again.json() is None, "a timed-out order must not be handed out again"
            late = await client.post(f"/api/v1/commands/{claimed['id']}/result", json=RESULT)
            assert late.status_code == 404, "a late result for a cleaned-up order is unknown"

    asyncio.run(run())


def test_a_result_before_the_timeout_returns_the_page_document() -> None:
    """[if] a claimant returns a result before timeout [then] POST answers 200 with the page result, [else stop]."""

    async def run() -> None:
        app = _app()
        async with _client(app) as client:
            await _open_page(client)
            order = asyncio.create_task(client.post("/api/v1/commands", json=PLAY))
            claimed = await _claim(client)
            done = await client.post(f"/api/v1/commands/{claimed['id']}/result", json=RESULT)
            assert done.status_code == 202
            answer = await asyncio.wait_for(order, 5.0)
            assert answer.status_code == 200
            body = answer.json()
            assert body["steps"] == RESULT["steps"]
            assert body["mirror_delta"] == RESULT["mirror_delta"]
            assert "reason" not in body
            assert _app_broker(app).pending == {}

    asyncio.run(run())


def test_an_in_process_single_command_timeout_also_cleans_up() -> None:
    """[if] an in-process single command is claimed and never answered [then] it raises 503 page_gone and holds nothing, [else stop]."""

    async def run() -> None:
        app = _app()
        broker = _app_broker(app)
        submitted = asyncio.create_task(submit_single_command_to_app(app, {"type": "play", "deck": 1}))
        await asyncio.sleep(0)
        assert broker.claim() is not None, "the in-process order must be claimable"
        with pytest.raises(HTTPException) as raised:
            await asyncio.wait_for(submitted, 5.0)
        assert raised.value.status_code == 503
        assert raised.value.detail["reason"] == PAGE_GONE_REASON
        assert broker.pending == {}
        assert broker.claim() is None, "a timed-out order must not be claimable again"

    asyncio.run(run())
