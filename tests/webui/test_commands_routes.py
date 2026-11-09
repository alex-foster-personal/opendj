"""AGENT-10047: an answered agent order carries its round-trip timing; GET /commands/timings aggregates the last 200.
[if] an answered order lacks timing, a 503 records a timing row, or the ring outgrows 200 [then] fail, [else stop].
"""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apps.webui.server.routes.commands import (
    ENGINE_QUITTING_REASON,
    NO_LEADER_REASON,
    ORDER_TIMINGS_MAX,
    PAGE_GONE_REASON,
    _app_broker,
    _order_type,
    _OrderBroker,
    close_order_broker,
    router,
)
from apps.webui.server.routes.state import router as state_router

pytestmark = pytest.mark.requirement("AGENT-10047")

PLAY = {"single": {"type": "play", "deck": 1, "playing": True}}
CUE = {"single": {"type": "cue", "deck": 1, "beat_offset": 0}}
RESULT = {"steps": [{"status": "succeeded"}], "mirror_delta": {"changed": {}}}


def _app(claim_deadline_s: float | None = None, result_deadline_s: float | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(state_router, prefix="/api/v1")
    app.include_router(router, prefix="/api/v1")
    if claim_deadline_s is not None:
        app.state.agent_order_claim_deadline_s = claim_deadline_s
    if result_deadline_s is not None:
        app.state.agent_order_result_deadline_s = result_deadline_s
    return app


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _open_page(client: AsyncClient) -> None:
    published = await client.put("/api/v1/state/ui-mirror", json={"client_open": True})
    assert published.status_code == 202


async def _page_answers(
    client: AsyncClient,
    app: FastAPI,
    claim_after_s: float,
    answer_after_s: float,
    steps: list | None = None,
) -> None:
    broker = _app_broker(app)
    deadline = time.monotonic() + 5.0
    while not broker.pending:
        if time.monotonic() > deadline:
            raise AssertionError("order never submitted")
        await asyncio.sleep(0.001)
    await asyncio.sleep(claim_after_s)
    claimed = await client.get("/api/v1/commands/next")
    assert claimed.status_code == 200
    assert claimed.json() is not None
    await asyncio.sleep(answer_after_s)
    body = {"steps": steps if steps is not None else [{"status": "succeeded"}], "mirror_delta": {}}
    done = await client.post(f"/api/v1/commands/{claimed.json()['id']}/result", json=body)
    assert done.status_code == 202


def test_answered_order_reports_claim_execute_and_total_ms() -> None:
    async def run() -> None:
        app = _app()
        async with _client(app) as client:
            await _open_page(client)
            post = asyncio.create_task(client.post("/api/v1/commands", json=PLAY))
            page = asyncio.create_task(_page_answers(client, app, 0.022, 0.032))
            answer = await asyncio.wait_for(post, 30.0)
            await page
            assert answer.status_code == 200
            body = answer.json()
            assert "steps" in body and "mirror_delta" in body
            timing = body["timing"]
            assert 20 <= timing["claim_ms"] < 60
            assert 30 <= timing["execute_ms"] < 70
            assert timing["total_ms"] >= timing["claim_ms"] + timing["execute_ms"] - 1

    asyncio.run(run())


def test_timings_after_three_orders_reports_count_and_percentiles_by_type() -> None:
    async def run() -> None:
        app = _app()
        async with _client(app) as client:
            await _open_page(client)
            for order_json in (PLAY, PLAY, CUE):
                page = asyncio.create_task(_page_answers(client, app, 0, 0))
                post = await client.post("/api/v1/commands", json=order_json)
                await page
                assert post.status_code == 200
            summary = await client.get("/api/v1/commands/timings")
            assert summary.status_code == 200
            doc = summary.json()
            assert doc["count"] == 3
            assert doc["max_orders"] == 200
            for field in ("total_ms", "claim_ms"):
                assert isinstance(doc[field]["p50"], (int, float))
                assert isinstance(doc[field]["p95"], (int, float))
            assert doc["by_type"]["play"]["count"] == 2
            assert doc["by_type"]["cue"]["count"] == 1
            for bucket in doc["by_type"].values():
                assert isinstance(bucket["total_ms"]["p50"], (int, float))
                assert isinstance(bucket["total_ms"]["p95"], (int, float))

    asyncio.run(run())


def test_empty_ring_reports_zero_and_null_percentiles() -> None:
    async def run() -> None:
        async with _client(_app()) as client:
            summary = await client.get("/api/v1/commands/timings")
            assert summary.status_code == 200
            doc = summary.json()
            assert doc["count"] == 0
            assert doc["total_ms"] == {"p50": None, "p95": None}
            assert doc["by_type"] == {}

    asyncio.run(run())


def test_ring_keeps_at_most_200() -> None:
    async def run() -> None:
        broker = _OrderBroker()
        for i in range(201):
            order_id, _ = broker.submit({"kind": "single", "payload": {"type": f"t{i}"}})
            broker.claim()
            broker.complete(order_id, {"steps": [], "mirror_delta": {}})
        assert len(broker.timings) == 200
        assert ORDER_TIMINGS_MAX == 200
        assert broker.timings_summary()["count"] == 200
        types = {t.type for t in broker.timings}
        assert "t0" not in types
        assert "t200" in types
        small = _OrderBroker(timings_max=3)
        for j in range(5):
            oid, _ = small.submit({"kind": "single", "payload": {"type": f"s{j}"}})
            small.claim()
            small.complete(oid, {"steps": [], "mirror_delta": {}})
        assert len(small.timings) == 3

    asyncio.run(run())


def test_no_leader_503_records_no_timing() -> None:
    async def run() -> None:
        async with _client(_app(claim_deadline_s=0.05)) as client:
            await _open_page(client)
            answer = await asyncio.wait_for(client.post("/api/v1/commands", json=PLAY), 5.0)
            assert answer.status_code == 503
            assert answer.json()["reason"] == NO_LEADER_REASON
            summary = await client.get("/api/v1/commands/timings")
            assert summary.json()["count"] == 0

    asyncio.run(run())


def test_page_gone_503_records_no_timing() -> None:
    async def run() -> None:
        async with _client(_app(result_deadline_s=0.1)) as client:
            await _open_page(client)
            post = asyncio.create_task(client.post("/api/v1/commands", json=PLAY))
            await asyncio.sleep(0.05)
            claimed = await client.get("/api/v1/commands/next")
            assert claimed.status_code == 200
            answer = await asyncio.wait_for(post, 5.0)
            assert answer.status_code == 503
            assert answer.json()["reason"] == PAGE_GONE_REASON
            assert (await client.get("/api/v1/commands/timings")).json()["count"] == 0
            late = await client.post(
                f"/api/v1/commands/{claimed.json()['id']}/result",
                json=RESULT,
            )
            assert late.status_code == 404
            assert (await client.get("/api/v1/commands/timings")).json()["count"] == 0

    asyncio.run(run())


def test_engine_quitting_records_no_timing() -> None:
    async def run() -> None:
        app = _app(claim_deadline_s=30.0)
        async with _client(app) as client:
            await _open_page(client)
            post = asyncio.create_task(client.post("/api/v1/commands", json=PLAY))
            await asyncio.sleep(0.1)
            close_order_broker(app)
            answer = await asyncio.wait_for(post, 5.0)
            assert answer.status_code == 503
            assert answer.json()["reason"] == ENGINE_QUITTING_REASON
            assert (await client.get("/api/v1/commands/timings")).json()["count"] == 0

    asyncio.run(run())


def test_order_type_buckets() -> None:
    assert _order_type({"kind": "single", "payload": {"type": "play"}}) == "play"
    assert _order_type({"kind": "single", "payload": {}}) == "single"
    assert _order_type({"kind": "sequence", "payload": []}) == "sequence"
