"""AGENT-03 order broker contract.

[if] a client posts a sequence [then ⛔] it is handed to the open performance
     page as one order and resolves only with its per-step result document.
[if] the performance page is closed [then ⛔] POST /commands fails explicitly.
[if] the performance page is closed [then ⛔] GET /commands/next answers 409
     too, which is the precondition the browser poll is gated on
     (`apps/webui/frontend/tests/unit/agent-order-poll.test.mjs`).
[if] a well-formed order is posted while a page IS open [then ⛔] it reaches
     that page, rather than being rejected as malformed before it is offered.
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apps.webui.server.routes.commands import _OrderBroker, router
from apps.webui.server.routes.state import router as state_router


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(state_router, prefix="/api/v1")
    app.include_router(router, prefix="/api/v1")
    return app


def test_commands_refuse_when_no_performance_page_is_open() -> None:
    async def run() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=_app()), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/commands", json={"single": {"type": "play", "deck": 1, "playing": True}}
            )
        assert response.status_code == 409
        assert response.json() == {"client_open": False}

    asyncio.run(run())


def test_sequence_returns_the_page_result_with_each_step_status() -> None:
    async def run() -> None:
        broker = _OrderBroker()
        order_id, result = broker.submit(
            {"kind": "sequence", "payload": [{"type": "play", "deck": 1, "playing": True}]}
        )
        claimed = broker.claim()
        assert claimed is not None
        assert claimed[0] == order_id
        broker.complete(
            order_id,
            {
                "steps": [{"status": "succeeded"}],
                "mirror_delta": {"changed": {"decks": {"1": {"playing": True}}}},
            },
        )
        response = await result
        assert response["steps"] == [{"status": "succeeded"}]
        assert response["mirror_delta"]["changed"]["decks"]["1"]["playing"] is True

    asyncio.run(run())


async def _open_page(client: AsyncClient) -> None:
    """Put a mirror on record, which is what makes /commands answer at all."""
    published = await client.put("/api/v1/state/ui-mirror", json={"client_open": True})
    assert published.status_code == 202


def test_next_command_refuses_when_no_performance_page_is_open() -> None:
    async def run() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=_app()), base_url="http://test"
        ) as client:
            response = await client.get("/api/v1/commands/next")
        assert response.status_code == 409
        assert response.json() == {"client_open": False}

    asyncio.run(run())


def test_a_well_formed_order_reaches_the_open_page_and_returns_its_result() -> None:
    """Regression for an order validator that rejected EVERY body it was given.

    `set(body) != 1` compares a set to an int, so it was true for every order
    ever posted and the route could only 422. Both prior tests missed it: one
    checks the closed-page refusal, which returns before validation, and the
    other drives the broker directly without going through the route.
    """

    async def run() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=_app()), base_url="http://test"
        ) as client:
            await _open_page(client)
            order = asyncio.create_task(
                client.post(
                    "/api/v1/commands",
                    json={"sequence": [{"type": "play", "deck": 1, "playing": True}]},
                )
            )
            claimed: dict[str, Any] | None = None
            for _ in range(200):
                await asyncio.sleep(0.01)
                nxt = await client.get("/api/v1/commands/next")
                assert nxt.status_code == 200
                if nxt.json() is not None:
                    claimed = nxt.json()
                    break
            assert claimed is not None, "the open page was never offered the order"
            assert claimed["kind"] == "sequence"

            accepted = await client.post(
                f"/api/v1/commands/{claimed['id']}/result",
                json={
                    "steps": [{"status": "succeeded"}],
                    "mirror_delta": {"changed": {"decks": {"1": {"playing": True}}}},
                },
            )
            assert accepted.status_code == 202
            response = await order
            assert response.status_code == 200
            assert response.json()["steps"] == [{"status": "succeeded"}]

    asyncio.run(run())


def test_an_order_that_is_not_exactly_one_declared_kind_is_refused() -> None:
    async def run() -> None:
        async with AsyncClient(
            transport=ASGITransport(app=_app()), base_url="http://test"
        ) as client:
            await _open_page(client)
            two = await client.post(
                "/api/v1/commands",
                json={"single": {"type": "play"}, "ramp": {"command": {"type": "eq"}}},
            )
            unknown = await client.post("/api/v1/commands", json={"teleport": {}})
            empty = await client.post("/api/v1/commands", json={})
        assert two.status_code == 422
        assert unknown.status_code == 422
        assert empty.status_code == 422

    asyncio.run(run())
