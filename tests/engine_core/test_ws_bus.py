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
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.websockets import WebSocket

from apps.engine_core.ws import (
    HELLO_TOPIC,
    QUEUE_SIZE,
    SLOW_CONSUMER_CODE,
    TOPIC_HEALTH_CHANGED,
    TOPIC_JOBS_UPDATED,
    TOPIC_LIBRARY_CHANGED,
    TOPICS,
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
    assert "ts" in hello
    # C8: EXACTLY these three, not "at least". The hello is the only place a
    # client learns what the bus carries, so a topic that quietly stops being
    # advertised is a contract break the old `in` assertion could not see.
    assert hello["payload"]["topics"] == [
        TOPIC_JOBS_UPDATED,
        TOPIC_HEALTH_CHANGED,
        TOPIC_LIBRARY_CHANGED,
    ]
    assert list(TOPICS) == hello["payload"]["topics"]


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


# ----- C1: the subscriber outlives nothing --------------------------------
class _SocketThatDiesBeforeHello:
    """A client that vanishes between ``accept()`` and the hello send.

    Starlette raises rather than returning when a send lands on a socket the
    peer already dropped, so this is the real shape of the failure, not a
    contrivance.
    """

    def __init__(self, hub: WsHub) -> None:
        self.app = SimpleNamespace(state=SimpleNamespace(event_hub=hub))
        self.accepted = False

    async def accept(self) -> None:
        self.accepted = True

    async def send_text(self, text: str) -> None:
        raise RuntimeError('Cannot call "send" once a close message has been sent.')

    async def receive_text(self) -> str:
        raise AssertionError("unreachable: the hello send already failed")

    async def close(self, code: int = 1000, reason: str = "") -> None:
        return None


def test_a_dead_client_at_hello_time_does_not_leak_its_subscriber() -> None:
    """The hub is the leak surface: every attach() pins a 256-frame queue and
    a slot in the fan-out loop until detach() runs."""

    async def drive() -> tuple[int, bool]:
        hub = WsHub(contract_rev="rev", engine_version="0")
        hub.bind(asyncio.get_running_loop())
        socket = _SocketThatDiesBeforeHello(hub)
        with pytest.raises(RuntimeError, match="once a close message"):
            await events_endpoint(cast(WebSocket, socket))
        return hub.subscriber_count, socket.accepted

    count, accepted = asyncio.run(drive())
    assert accepted is True, "the failure must happen after accept, not before"
    assert count == 0, "attach() without a matching detach() pins the queue forever"


def test_a_normal_disconnect_still_detaches() -> None:
    """The C1 guard must not cost the ordinary path its cleanup."""
    hub = WsHub(contract_rev="rev", engine_version="0")
    with TestClient(_app(hub)) as client:
        with client.websocket_connect(EVENTS_PATH) as socket:
            socket.receive_text()
            assert hub.subscriber_count == 1
        # The endpoint's finally runs on the server side; give the loop the
        # hop it needs to notice the close before reading the count.
        with client.websocket_connect(EVENTS_PATH) as socket:
            socket.receive_text()
    assert hub.subscriber_count == 0


# ----- C4: a frame nobody can parse is worse than a loud failure ----------
def test_publishing_a_nan_payload_fails_loud_and_emits_no_frame() -> None:
    """Python's json defaults to allow_nan=True and emits the bare token
    ``NaN``, which JSON.parse rejects. The client cannot tell that frame from
    a dropped one, so it resyncs on every status transition, forever."""

    async def drive() -> tuple[int, int, str]:
        hub = WsHub(contract_rev="rev", engine_version="0")
        hub.bind(asyncio.get_running_loop())
        subscriber, _hello = hub.attach()
        with pytest.raises(ValueError) as caught:
            hub.publish(TOPIC_JOBS_UPDATED, {"x": float("nan")})
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return subscriber.queue.qsize(), hub.seq, str(caught.value)

    queued, seq, message = asyncio.run(drive())
    assert TOPIC_JOBS_UPDATED in message, "the error must name the topic that broke"
    assert queued == 0, "a payload that cannot be serialized must emit nothing"
    assert seq == 0, "a rejected publish must not burn a seq the client will miss"


def test_infinity_is_rejected_on_the_same_grounds_as_nan() -> None:
    async def drive() -> str:
        hub = WsHub(contract_rev="rev", engine_version="0")
        hub.bind(asyncio.get_running_loop())
        with pytest.raises(ValueError) as caught:
            hub.publish(TOPIC_HEALTH_CHANGED, {"ratio": float("inf")})
        return str(caught.value)

    assert TOPIC_HEALTH_CHANGED in asyncio.run(drive())


def test_a_finite_float_payload_still_publishes() -> None:
    """allow_nan=False must reject only the non-JSON tokens."""

    async def drive() -> dict[str, Any]:
        hub = WsHub(contract_rev="rev", engine_version="0")
        hub.bind(asyncio.get_running_loop())
        subscriber, _hello = hub.attach()
        hub.publish(TOPIC_JOBS_UPDATED, {"ratio": 0.5})
        await asyncio.sleep(0)
        return cast(dict[str, Any], json.loads(subscriber.queue.get_nowait()))

    frame = asyncio.run(drive())
    assert frame["payload"] == {"ratio": 0.5}


# ----- C8: the module exports what its consumers import -------------------
def test_every_published_topic_is_exported() -> None:
    """TOPIC_LIBRARY_CHANGED was missing from __all__ while its two siblings
    were exported, so `from ... import *` gave a partial contract."""
    from apps.engine_core import ws

    for topic_name in (
        "TOPIC_JOBS_UPDATED",
        "TOPIC_HEALTH_CHANGED",
        "TOPIC_LIBRARY_CHANGED",
    ):
        assert topic_name in ws.__all__, f"{topic_name} is published but not exported"
