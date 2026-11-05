"""AGENT-19: the order claim is a long poll, so a hidden leader tab gets orders at once.
[if] an order is posted while a claim is held [then] the claim answers at once, [else stop].

A hidden tab throttles timers (Chrome: 1 s, a timer chain once a minute after five
hidden minutes), so the page's 50 ms claim poll paid that per order. A held network
request is not throttled, so the engine now holds `GET /commands/next?wait_ms=N`
and answers the moment an order is submitted.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apps.webui.server.routes.commands import (
    CLAIM_LEASE_MAX_AGE_S,
    ORDER_WAIT_HEADER,
    ORDER_WAIT_MAX_MS,
    _OrderBroker,
    router,
)
from apps.webui.server.routes.state import MirrorLease
from apps.webui.server.routes.state import router as state_router

pytestmark = pytest.mark.requirement("AGENT-19")

PLAY = {"single": {"type": "play", "deck": 1, "playing": True}}

ARMED = {"autoplay_enabled": True, "autoplay_armed": True, "autoplay_disarm_reason": None}
LEASE_DOC = {"client_open": True, "client_id": "tab-a", "decks": {}, "mirror_schema": 2, **ARMED}


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(state_router, prefix="/api/v1")
    app.include_router(router, prefix="/api/v1")
    return app


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _open_page(client: AsyncClient) -> None:
    published = await client.put("/api/v1/state/ui-mirror", json={"client_open": True})
    assert published.status_code == 202


async def _open_leased_page(client: AsyncClient, tab: str) -> None:
    doc = {**LEASE_DOC, "client_id": tab}
    published = await client.put("/api/v1/state/ui-mirror", json=doc, headers={"x-opendj-lease": tab})
    assert published.status_code == 202


async def _never_gone() -> bool:
    return False


async def _gone() -> bool:
    return True


def test_a_held_claim_answers_as_soon_as_an_order_is_posted() -> None:
    """[if] an order is posted during a 5 s hold [then] the claim gets it within 1 s, [else stop]."""

    async def run() -> None:
        async with _client(_app()) as client:
            await _open_page(client)
            started = time.monotonic()
            claim = asyncio.create_task(client.get("/api/v1/commands/next", params={"wait_ms": 5000}))
            await asyncio.sleep(0.2)
            assert not claim.done(), "the engine must HOLD an empty claim, not answer at once"
            order = asyncio.create_task(client.post("/api/v1/commands", json=PLAY))
            claimed = await claim
            elapsed = time.monotonic() - started
            assert claimed.status_code == 200
            assert claimed.headers[ORDER_WAIT_HEADER] == "5000"
            body = claimed.json()
            assert body["kind"] == "single"
            assert elapsed < 1.0, f"held claim took {elapsed:.2f}s after the order was posted"
            done = await client.post(
                f"/api/v1/commands/{body['id']}/result",
                json={"steps": [{"status": "succeeded"}], "mirror_delta": {"changed": {}}},
            )
            assert done.status_code == 202
            assert (await order).json()["steps"] == [{"status": "succeeded"}]

    asyncio.run(run())


def test_an_empty_hold_ends_after_the_window_with_the_honoured_header() -> None:
    """[if] a 300 ms hold sees no order [then] null after ~300 ms with the header, [else stop]."""

    async def run() -> None:
        async with _client(_app()) as client:
            await _open_page(client)
            started = time.monotonic()
            claimed = await client.get("/api/v1/commands/next", params={"wait_ms": 300})
            elapsed = time.monotonic() - started
            assert claimed.status_code == 200
            assert claimed.json() is None
            assert claimed.headers[ORDER_WAIT_HEADER] == "300"
            assert 0.25 <= elapsed < 2.0, f"hold lasted {elapsed:.2f}s, wanted about 0.3s"

    asyncio.run(run())


def test_the_default_claim_still_answers_at_once() -> None:
    """[if] wait_ms is omitted [then] the AGENT-03 claim answers immediately, [else stop]."""

    async def run() -> None:
        async with _client(_app()) as client:
            await _open_page(client)
            started = time.monotonic()
            claimed = await client.get("/api/v1/commands/next")
            assert claimed.status_code == 200
            assert claimed.json() is None
            assert claimed.headers[ORDER_WAIT_HEADER] == "0"
            assert time.monotonic() - started < 0.5

    asyncio.run(run())


def test_a_claimant_that_left_mid_hold_does_not_swallow_the_order() -> None:
    """[if] the claimant disconnects mid-hold [then] the order stays claimable, [else stop]."""

    async def run() -> None:
        broker = _OrderBroker()
        held = asyncio.create_task(broker.claim_within(5.0, _gone))
        await asyncio.sleep(0.05)
        order_id, _result = broker.submit({"kind": "single", "payload": {"type": "play"}})
        assert await held is None, "a gone claimant must not take the order"
        again = await broker.claim_within(0.0, _never_gone)
        assert again is not None and again[0] == order_id

    asyncio.run(run())


def test_a_live_claimant_takes_the_order_submitted_mid_hold() -> None:
    """[if] an order arrives while a live claimant holds [then] that claimant gets it, [else stop]."""

    async def run() -> None:
        broker = _OrderBroker()
        held = asyncio.create_task(broker.claim_within(5.0, _never_gone))
        await asyncio.sleep(0.05)
        order_id, _result = broker.submit({"kind": "single", "payload": {"type": "play"}})
        claimed = await asyncio.wait_for(held, 1.0)
        assert claimed is not None and claimed[0] == order_id

    asyncio.run(run())


def test_a_mirror_closed_mid_hold_answers_409() -> None:
    """[if] the page's mirror is closed during a hold [then] the claim answers 409, [else stop]."""

    async def run() -> None:
        async with _client(_app()) as client:
            await _open_page(client)
            claim = asyncio.create_task(client.get("/api/v1/commands/next", params={"wait_ms": 300}))
            await asyncio.sleep(0.05)
            closed = await client.delete("/api/v1/state/ui-mirror")
            assert closed.status_code in {200, 204}
            claimed = await claim
            assert claimed.status_code == 409
            assert claimed.json() == {"client_open": False}

    asyncio.run(run())


def test_wait_ms_is_bounded_and_refused_for_the_shell_consumer() -> None:
    """[if] wait_ms exceeds the cap or targets the shell consumer [then] 422, [else stop]."""

    async def run() -> None:
        async with _client(_app()) as client:
            await _open_page(client)
            too_long = await client.get("/api/v1/commands/next", params={"wait_ms": ORDER_WAIT_MAX_MS + 1})
            assert too_long.status_code == 422
            shell = await client.get("/api/v1/commands/next", params={"consumer": "shell", "wait_ms": 100})
            assert shell.status_code == 422

    asyncio.run(run())


@pytest.mark.requirement("AGENT-8408")
def test_a_refused_lease_holder_does_not_claim_the_order() -> None:
    """[if] GET /next names the wrong lease holder [then] 409 lease_held and the order stays unclaimed, [else stop]."""

    async def run() -> None:
        async with _client(_app()) as client:
            await _open_leased_page(client, "tab-a")
            order = asyncio.create_task(client.post("/api/v1/commands", json=PLAY))
            await asyncio.sleep(0.05)
            wrong = await client.get("/api/v1/commands/next", headers={"x-opendj-lease": "tab-b"})
            assert wrong.status_code == 409
            assert wrong.json()["reason"] == "lease_held"
            right = await client.get("/api/v1/commands/next", headers={"x-opendj-lease": "tab-a"})
            assert right.status_code == 200
            body = right.json()
            assert body is not None and body["kind"] == "single"
            done = await client.post(
                f"/api/v1/commands/{body['id']}/result",
                json={"steps": [{"status": "succeeded"}], "mirror_delta": {"changed": {}}},
            )
            assert done.status_code == 202
            assert (await order).status_code == 200

    asyncio.run(run())


@pytest.mark.requirement("AGENT-8408")
def test_the_matching_lease_holder_claims() -> None:
    """[if] GET /next names the live holder [then] 200 with the order, [else stop]."""

    async def run() -> None:
        async with _client(_app()) as client:
            await _open_leased_page(client, "tab-a")
            order = asyncio.create_task(client.post("/api/v1/commands", json=PLAY))
            await asyncio.sleep(0.05)
            claimed = await client.get("/api/v1/commands/next", headers={"x-opendj-lease": "tab-a"})
            assert claimed.status_code == 200
            body = claimed.json()
            assert body is not None
            done = await client.post(
                f"/api/v1/commands/{body['id']}/result",
                json={"steps": [{"status": "succeeded"}], "mirror_delta": {"changed": {}}},
            )
            assert done.status_code == 202
            assert (await order).status_code == 200

    asyncio.run(run())


@pytest.mark.requirement("AGENT-8408")
def test_no_lease_header_on_get_still_claims_with_a_leased_mirror() -> None:
    """[if] the mirror was opened with a lease but GET omits the header [then] 200, [else stop]."""

    async def run() -> None:
        async with _client(_app()) as client:
            await _open_leased_page(client, "tab-a")
            order = asyncio.create_task(client.post("/api/v1/commands", json=PLAY))
            await asyncio.sleep(0.05)
            claimed = await client.get("/api/v1/commands/next")
            assert claimed.status_code == 200
            assert claimed.json() is not None
            body = claimed.json()
            done = await client.post(
                f"/api/v1/commands/{body['id']}/result",
                json={"steps": [{"status": "succeeded"}], "mirror_delta": {"changed": {}}},
            )
            assert done.status_code == 202
            assert (await order).status_code == 200

    asyncio.run(run())


@pytest.mark.requirement("AGENT-8408")
def test_no_lease_header_without_a_leased_mirror_is_unchanged() -> None:
    """[if] the page opened without a lease header on PUT [then] GET without header still works, [else stop]."""

    async def run() -> None:
        async with _client(_app()) as client:
            await _open_page(client)
            claimed = await client.get("/api/v1/commands/next")
            assert claimed.status_code == 200
            assert claimed.json() is None

    asyncio.run(run())


@pytest.mark.requirement("AGENT-8408")
def test_a_held_wrong_holder_gets_409_after_the_lease_moves() -> None:
    """[if] tab-a holds GET /next then tab-b takes the lease [then] tab-a gets 409 and tab-b claims, [else stop]."""

    async def run() -> None:
        async with _client(_app()) as client:
            await _open_leased_page(client, "tab-a")
            held = asyncio.create_task(
                client.get("/api/v1/commands/next", params={"wait_ms": 5000}, headers={"x-opendj-lease": "tab-a"})
            )
            await asyncio.sleep(0.05)
            doc_b = {**LEASE_DOC, "client_id": "tab-b"}
            taken = await client.put(
                "/api/v1/state/ui-mirror",
                json=doc_b,
                headers={"x-opendj-lease": "tab-b", "x-opendj-lease-takeover": "1"},
            )
            assert taken.status_code == 202
            order = asyncio.create_task(client.post("/api/v1/commands", json=PLAY))
            await asyncio.sleep(0.05)
            stale = await held
            assert stale.status_code == 409
            fresh = await client.get("/api/v1/commands/next", headers={"x-opendj-lease": "tab-b"})
            assert fresh.status_code == 200
            body = fresh.json()
            assert body is not None
            done = await client.post(
                f"/api/v1/commands/{body['id']}/result",
                json={"steps": [{"status": "succeeded"}], "mirror_delta": {"changed": {}}},
            )
            assert done.status_code == 202
            assert (await order).status_code == 200

    asyncio.run(run())


@pytest.mark.requirement("AGENT-8408")
def test_an_immediate_wrong_holder_gets_409_and_the_order_stays_claimable() -> None:
    """[if] an order exists and the wrong holder GETs first [then] 409 and a later GET claims, [else stop]."""

    async def run() -> None:
        async with _client(_app()) as client:
            await _open_leased_page(client, "tab-a")
            order = asyncio.create_task(client.post("/api/v1/commands", json=PLAY))
            await asyncio.sleep(0.05)
            wrong = await client.get("/api/v1/commands/next", headers={"x-opendj-lease": "tab-b"})
            assert wrong.status_code == 409
            right = await client.get("/api/v1/commands/next", headers={"x-opendj-lease": "tab-a"})
            assert right.status_code == 200
            body = right.json()
            assert body is not None
            done = await client.post(
                f"/api/v1/commands/{body['id']}/result",
                json={"steps": [{"status": "succeeded"}], "mirror_delta": {"changed": {}}},
            )
            assert done.status_code == 202
            assert (await order).status_code == 200

    asyncio.run(run())


@pytest.mark.requirement("AGENT-8408")
def test_a_stale_lease_renewal_is_refused() -> None:
    """[if] the holder's lease was not renewed within CLAIM_LEASE_MAX_AGE_S [then] 409 lease_stale, [else stop]."""

    async def run() -> None:
        app = _app()
        async with _client(app) as client:
            await _open_leased_page(client, "tab-a")
            app.state.ui_mirror_lease = MirrorLease(holder="tab-a", expires_monotonic=time.monotonic() + 1.0)
            stale = await client.get("/api/v1/commands/next", headers={"x-opendj-lease": "tab-a"})
            assert stale.status_code == 409
            assert stale.json()["reason"] == "lease_stale"

    asyncio.run(run())


@pytest.mark.requirement("AGENT-8408")
def test_may_claim_false_does_not_take_a_submitted_order() -> None:
    """[if] may_claim is always false [then] claim_within does not claim, [else stop]."""

    async def run() -> None:
        broker = _OrderBroker()
        order_id, _result = broker.submit({"kind": "single", "payload": {"type": "play"}})
        claimed = await broker.claim_within(0.0, _never_gone, may_claim=lambda: False)
        assert claimed is None
        again = await broker.claim_within(0.0, _never_gone)
        assert again is not None and again[0] == order_id

    asyncio.run(run())


@pytest.mark.requirement("AGENT-8408")
def test_claim_lease_max_age_matches_the_named_constant() -> None:
    """[if] the freshness window is documented [then] CLAIM_LEASE_MAX_AGE_S is 3 s, [else stop]."""
    assert CLAIM_LEASE_MAX_AGE_S == 3.0
