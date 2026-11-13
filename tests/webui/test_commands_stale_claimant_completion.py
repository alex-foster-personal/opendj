"""A closed claimant's late completion cannot touch a newer live claimant's order.

Race regression for ``(private path)`` ("Scope and
diagnosis", issue #13210): a page claims an order, closes before answering, a
second live page takes the lease and polls for work, and the closed page's
result finally lands. That late result must not complete, overwrite or strand
the order the live page now owns.

[if] a claimant disconnects after claiming [then] the live claimant still gets and owns new work, [else stop].
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apps.webui.server.routes.commands import PAGE_GONE_REASON, router
from apps.webui.server.routes.state import router as state_router

pytestmark = pytest.mark.requirement("AGENT-8408")

PLAY = {"single": {"type": "play", "deck": 1, "playing": True}}
STOP = {"single": {"type": "play", "deck": 2, "playing": False}}
ARMED = {"autoplay_enabled": True, "autoplay_armed": True, "autoplay_disarm_reason": None}
LEASE_DOC = {"client_open": True, "decks": {}, "mirror_schema": 2, **ARMED}
STALE_RESULT = {"steps": [{"status": "succeeded", "by": "tab-a"}], "mirror_delta": {"changed": {"from": "tab-a"}}}
LIVE_RESULT = {"steps": [{"status": "succeeded", "by": "tab-b"}], "mirror_delta": {"changed": {"from": "tab-b"}}}


def _app(result_deadline_s: float = 30.0) -> FastAPI:
    """An engine app with the state and commands routers and a chosen result deadline."""
    app = FastAPI()
    app.include_router(state_router, prefix="/api/v1")
    app.include_router(router, prefix="/api/v1")
    app.state.agent_order_claim_deadline_s = 5.0
    app.state.agent_order_result_deadline_s = result_deadline_s
    return app


def _client(app: FastAPI) -> AsyncClient:
    """An in-process HTTP client for ``app``."""
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _publish(client: AsyncClient, tab: str, takeover: bool = False) -> None:
    """Publish the mirror as ``tab``, taking the lease over when asked."""
    headers = {"x-opendj-lease": tab}
    if takeover:
        headers["x-opendj-lease-takeover"] = "1"
    published = await client.put("/api/v1/state/ui-mirror", json={**LEASE_DOC, "client_id": tab}, headers=headers)
    assert published.status_code == 202


async def _claim(client: AsyncClient, tab: str, wait_ms: int = 0) -> dict[str, object]:
    """Claim the next order as ``tab`` and return its body (it must be an order)."""
    answer = await client.get("/api/v1/commands/next", params={"wait_ms": wait_ms}, headers={"x-opendj-lease": tab})
    assert answer.status_code == 200
    body = answer.json()
    assert isinstance(body, dict), f"{tab} expected an order, got {body!r}"
    return body


def test_a_late_completion_after_page_gone_cannot_complete_the_live_claimants_order() -> None:
    """[if] tab-a claims, closes, tab-b claims a new order and tab-a posts late [then] 404 and tab-b's order is untouched, [else stop]."""

    async def run() -> None:
        async with _client(_app(result_deadline_s=0.3)) as client:
            await _publish(client, "tab-a")
            first = asyncio.create_task(client.post("/api/v1/commands", json=PLAY))
            stale_order = await _claim(client, "tab-a", wait_ms=2000)
            # tab-a closes without answering; its order is withdrawn as page_gone.
            gone = await asyncio.wait_for(first, 5.0)
            assert gone.status_code == 503
            assert gone.json()["reason"] == PAGE_GONE_REASON

            await _publish(client, "tab-b", takeover=True)
            second = asyncio.create_task(client.post("/api/v1/commands", json=STOP))
            live_order = await _claim(client, "tab-b", wait_ms=2000)
            assert live_order["id"] != stale_order["id"]
            assert live_order["payload"] == STOP["single"]

            late = await client.post(f"/api/v1/commands/{stale_order['id']}/result", json=STALE_RESULT)
            assert late.status_code == 404
            await asyncio.sleep(0.05)
            assert not second.done(), "a stale completion must not answer the live claimant's order"

            refused = await client.get("/api/v1/commands/next", headers={"x-opendj-lease": "tab-a"})
            assert refused.status_code == 409
            assert refused.json()["reason"] == "lease_held"

            done = await client.post(f"/api/v1/commands/{live_order['id']}/result", json=LIVE_RESULT)
            assert done.status_code == 202
            answer = await asyncio.wait_for(second, 5.0)
            assert answer.status_code == 200
            body = answer.json()
            assert body["steps"] == LIVE_RESULT["steps"]
            assert body["mirror_delta"] == LIVE_RESULT["mirror_delta"]
            assert (await client.get("/api/v1/commands/timings")).json()["count"] == 1

    asyncio.run(run())


def test_a_stranded_claim_does_not_block_or_leak_into_the_live_claimants_order() -> None:
    """[if] tab-a's claim is still unanswered when tab-b polls and tab-a then answers [then] each order keeps its own result, [else stop]."""

    async def run() -> None:
        async with _client(_app()) as client:
            await _publish(client, "tab-a")
            first = asyncio.create_task(client.post("/api/v1/commands", json=PLAY))
            stale_order = await _claim(client, "tab-a", wait_ms=2000)

            # tab-a has closed (lease taken over) while its order is still claimed.
            await _publish(client, "tab-b", takeover=True)
            held = asyncio.create_task(
                client.get("/api/v1/commands/next", params={"wait_ms": 2000}, headers={"x-opendj-lease": "tab-b"})
            )
            await asyncio.sleep(0.05)
            assert not held.done(), "tab-b must not be handed tab-a's already claimed order"
            second = asyncio.create_task(client.post("/api/v1/commands", json=STOP))
            live = await asyncio.wait_for(held, 5.0)
            assert live.status_code == 200
            live_order = live.json()
            assert live_order["id"] != stale_order["id"]
            assert live_order["payload"] == STOP["single"]

            late = await client.post(f"/api/v1/commands/{stale_order['id']}/result", json=STALE_RESULT)
            assert late.status_code == 202
            stale_answer = await asyncio.wait_for(first, 5.0)
            assert stale_answer.json()["mirror_delta"] == STALE_RESULT["mirror_delta"]
            await asyncio.sleep(0.05)
            assert not second.done(), "tab-a's late result must not answer tab-b's order"

            again = await client.post(f"/api/v1/commands/{stale_order['id']}/result", json=STALE_RESULT)
            assert again.status_code == 404
            assert not second.done()

            done = await client.post(f"/api/v1/commands/{live_order['id']}/result", json=LIVE_RESULT)
            assert done.status_code == 202
            answer = await asyncio.wait_for(second, 5.0)
            assert answer.status_code == 200
            assert answer.json()["mirror_delta"] == LIVE_RESULT["mirror_delta"]

    asyncio.run(run())
