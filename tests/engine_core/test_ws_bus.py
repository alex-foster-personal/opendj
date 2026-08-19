"""WS bus: hello handshake, seq monotonicity, and fail-loud backpressure.

The app here is a real FastAPI app carrying the real ``WsHub`` and the real
``events_endpoint`` -- only the legacy routers are absent, because none of
them participate in the event contract. Nothing is stubbed.

The ``/burst`` route exists so the publishes happen ON the engine loop. Every
``publish`` schedules its dispatch via ``call_soon``, and the loop's ready
queue is FIFO, so a burst issued without awaiting is fully dispatched before
the pump's wakeup runs. That is what makes the overflow assertion
deterministic instead of a race.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.testclient import TestClient

from apps.engine_core.ws import (
    HELLO_TOPIC,
    QUEUE_SIZE,
    SLOW_CONSUMER_CODE,
    TOPIC_JOBS_UPDATED,
    WsHub,
    events_endpoint,
)

EVENTS_PATH = "/api/v1/events"


def _app(hub: WsHub) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        hub.bind(asyncio.get_running_loop())
        yield
        hub.unbind()

    app = FastAPI(lifespan=lifespan)
    app.state.event_hub = hub
    app.add_api_websocket_route(EVENTS_PATH, events_endpoint, name="events")

    @app.post("/burst")
    async def burst(count: int) -> dict[str, int]:
        for index in range(count):
            hub.publish(TOPIC_JOBS_UPDATED, {"id": f"job-{index}"})
        return {"published": count}

    return app


def test_hello_frame_carries_the_contract_and_seq_start() -> None:
    hub = WsHub(contract_rev="abc123abc123", engine_version="9.9.9")
    with TestClient(_app(hub)) as client, client.websocket_connect(
        EVENTS_PATH
    ) as socket:
        hello = json.loads(socket.receive_text())

    assert hello["topic"] == HELLO_TOPIC
    assert hello["payload"]["contract_rev"] == "abc123abc123"
    assert hello["payload"]["engine_version"] == "9.9.9"
    assert hello["payload"]["seq_start"] == 0
    assert TOPIC_JOBS_UPDATED in hello["payload"]["topics"]
    assert "ts" in hello


def test_seq_is_lifetime_monotonic_across_frames() -> None:
    hub = WsHub(contract_rev="rev", engine_version="0")
    with TestClient(_app(hub)) as client, client.websocket_connect(
        EVENTS_PATH
    ) as socket:
        hello = json.loads(socket.receive_text())
        client.post("/burst", params={"count": 5})
        frames = [json.loads(socket.receive_text()) for _ in range(5)]

    seqs = [frame["seq"] for frame in frames]
    assert seqs == [hello["payload"]["seq_start"] + n for n in range(1, 6)]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)
    assert {frame["topic"] for frame in frames} == {TOPIC_JOBS_UPDATED}
    assert frames[0]["payload"] == {"id": "job-0"}


def test_seq_keeps_climbing_for_a_second_connection() -> None:
    """seq is engine-lifetime, not per-connection: a reconnecting client
    must be able to tell it missed events."""
    hub = WsHub(contract_rev="rev", engine_version="0")
    app = _app(hub)
    with TestClient(app) as client:
        with client.websocket_connect(EVENTS_PATH) as socket:
            socket.receive_text()
            client.post("/burst", params={"count": 3})
            last = json.loads(socket.receive_text())
            socket.receive_text()
            socket.receive_text()
        with client.websocket_connect(EVENTS_PATH) as socket:
            hello = json.loads(socket.receive_text())

    assert last["seq"] == 1
    assert hello["payload"]["seq_start"] == 3


def test_slow_consumer_is_closed_with_1013_not_silently_dropped() -> None:
    hub = WsHub(contract_rev="rev", engine_version="0", queue_size=4)
    with TestClient(_app(hub)) as client, client.websocket_connect(
        EVENTS_PATH
    ) as socket:
        socket.receive_text()
        client.post("/burst", params={"count": 64})
        codes = []
        for _ in range(64):
            message = socket.receive()
            if message["type"] == "websocket.close":
                codes.append(message["code"])
                break
    assert codes == [SLOW_CONSUMER_CODE]


def test_overflow_never_grows_the_queue_past_its_bound() -> None:
    """The bound is the point: an unbounded buffer would trade a visible
    disconnect for an invisible leak."""

    async def drive() -> tuple[int, bool]:
        hub = WsHub(contract_rev="rev", engine_version="0")
        hub.bind(asyncio.get_running_loop())
        subscriber, _hello = hub.attach()
        for index in range(QUEUE_SIZE * 2):
            hub.publish(TOPIC_JOBS_UPDATED, {"id": index})
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return subscriber.queue.qsize(), subscriber.overflow.is_set()

    size, overflowed = asyncio.run(drive())
    assert size == QUEUE_SIZE
    assert overflowed is True
