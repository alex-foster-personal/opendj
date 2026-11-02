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
from starlette.websockets import WebSocket, WebSocketDisconnect

from apps.engine_core.ws import (
    HELLO_TOPIC,
    QUEUE_SIZE,
    SHELL_ACK_PROTOCOL_ERROR_CODE,
    SLOW_CONSUMER_CODE,
    TOPIC_HEALTH_CHANGED,
    TOPIC_ANALYSIS_CHANGED,
    TOPIC_JOBS_UPDATED,
    TOPIC_LIBRARY_CHANGED,
    TOPIC_SHELL_QUIT,
    TOPIC_SHELL_QUIT_ACK,
    TOPICS,
    ShellAckProtocolError,
    Subscriber,
    WsHub,
    events_endpoint,
)
from tests.support.process_probes import wait_for

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
        TOPIC_ANALYSIS_CHANGED,
    ]
    assert list(TOPICS) == hello["payload"]["topics"]


def test_analysis_changed_frame_names_track_and_lane() -> None:
    hub = WsHub(contract_rev="rev", engine_version="0")
    with TestClient(_app(hub)) as client, client.websocket_connect(
        EVENTS_PATH
    ) as socket:
        socket.receive_text()
        hub.publish(
            TOPIC_ANALYSIS_CHANGED,
            {"stable_id": "track-1", "lane": "beatgrid"},
        )
        frame = json.loads(socket.receive_text())

    assert frame["topic"] == TOPIC_ANALYSIS_CHANGED
    assert frame["payload"] == {"stable_id": "track-1", "lane": "beatgrid"}


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


def test_shell_quit_published_before_any_subscriber_replays_on_attach() -> None:
    """[if] lifecycle/quit fires before /events attaches [then] shell.quit is
    delivered on connect, [else stop]."""
    hub = WsHub(contract_rev="rev", engine_version="0")
    with TestClient(_app(hub)) as client:
        hub.publish(TOPIC_SHELL_QUIT, {"force": True, "source": "lifecycle"})
        assert hub.seq == 0
        with client.websocket_connect(f"{EVENTS_PATH}?consumer=shell") as socket:
            hello = json.loads(socket.receive_text())
            quit_frame = json.loads(socket.receive_text())

    assert hello["topic"] == HELLO_TOPIC
    assert hello["payload"]["seq_start"] == 0
    assert quit_frame["topic"] == TOPIC_SHELL_QUIT
    assert quit_frame["seq"] == 1
    assert quit_frame["payload"] == {"force": True, "source": "lifecycle"}


def test_shell_quit_replay_stays_pending_until_shell_ack() -> None:
    """[if] a retained quit is replayed on shell attach [then] pending stays until
    ack, [else stop]."""
    hub = WsHub(contract_rev="rev", engine_version="0")
    with TestClient(_app(hub)) as client:
        hub.publish(TOPIC_SHELL_QUIT, {"force": True, "source": "lifecycle"})
        with client.websocket_connect(f"{EVENTS_PATH}?consumer=shell") as socket:
            socket.receive_text()
            quit_frame = json.loads(socket.receive_text())
            assert hub._pending_shell_quit_body is not None
            socket.send_text(
                json.dumps({"topic": TOPIC_SHELL_QUIT_ACK, "seq": quit_frame["seq"]})
            )
            # The ack is handled on the server loop and nothing is sent back, so
            # poll until that loop has run it instead of racing it.
            wait_for(
                lambda: hub._pending_shell_quit_body is None,
                timeout_s=2.0,
                what="the server to clear the pending shell.quit after the ack",
            )


def test_shell_quit_stays_pending_while_shell_subscriber_is_not_ready() -> None:
    """[if] consumer=shell is attached but hello has not completed [then] a new
    shell.quit stays pending, [else stop]."""

    async def drive() -> tuple[str | None, int, int]:
        hub = WsHub(contract_rev="rev", engine_version="0")
        hub.bind(asyncio.get_running_loop())
        subscriber = Subscriber(
            queue=asyncio.Queue(maxsize=QUEUE_SIZE),
            overflow=asyncio.Event(),
            delivers_shell_quit=True,
            ready=False,
        )
        hub._subscribers.add(subscriber)
        hub.publish(TOPIC_SHELL_QUIT, {"force": True, "source": "lifecycle"})
        await asyncio.sleep(0)
        return (
            hub._pending_shell_quit_body,
            hub.seq,
            subscriber.queue.qsize(),
        )

    pending, seq, queued = asyncio.run(drive())
    assert pending is not None
    assert '"source": "lifecycle"' in pending
    assert seq == 0
    assert queued == 0


def test_shell_quit_live_dispatch_stays_pending_until_shell_ack() -> None:
    """[if] shell.quit is queued on a live shell socket [then] pending stays until
    ack, [else stop]."""

    async def drive() -> tuple[str | None, str | None]:
        hub = WsHub(contract_rev="rev", engine_version="0")
        hub.bind(asyncio.get_running_loop())
        subscriber = Subscriber(
            queue=asyncio.Queue(maxsize=QUEUE_SIZE),
            overflow=asyncio.Event(),
            delivers_shell_quit=True,
            ready=True,
        )
        hub._subscribers.add(subscriber)
        hub.publish(TOPIC_SHELL_QUIT, {"force": True, "source": "lifecycle"})
        await asyncio.sleep(0)
        pending_before_ack = hub._pending_shell_quit_body
        hub.handle_shell_quit_ack(
            subscriber,
            json.dumps({"topic": TOPIC_SHELL_QUIT_ACK, "seq": 1}),
        )
        await asyncio.sleep(0)
        return pending_before_ack, hub._pending_shell_quit_body

    pending_before_ack, pending_after_ack = asyncio.run(drive())
    assert pending_before_ack is not None
    assert pending_after_ack is None


def test_shell_quit_stale_ack_does_not_clear_newer_retained_quit() -> None:
    """[if] two lifecycle quits land before ack [then] only the latest seq clears,
    [else stop]."""

    async def drive() -> tuple[int | None, str | None]:
        hub = WsHub(contract_rev="rev", engine_version="0")
        hub.bind(asyncio.get_running_loop())
        subscriber = Subscriber(
            queue=asyncio.Queue(maxsize=QUEUE_SIZE),
            overflow=asyncio.Event(),
            delivers_shell_quit=True,
            ready=True,
        )
        hub._subscribers.add(subscriber)
        hub.publish(TOPIC_SHELL_QUIT, {"force": True, "source": "first"})
        await asyncio.sleep(0)
        first_seq = hub._pending_shell_quit_seq
        hub.publish(TOPIC_SHELL_QUIT, {"force": True, "source": "second"})
        await asyncio.sleep(0)
        hub.handle_shell_quit_ack(
            subscriber,
            json.dumps({"topic": TOPIC_SHELL_QUIT_ACK, "seq": first_seq}),
        )
        await asyncio.sleep(0)
        return hub._pending_shell_quit_seq, hub._pending_shell_quit_body

    pending_seq, pending_body = asyncio.run(drive())
    assert pending_seq is not None
    assert pending_body is not None
    assert '"source": "second"' in pending_body


def test_shell_quit_live_publish_while_shell_socket_not_ready_stays_retained() -> None:
    """[if] consumer=shell is connected but not ready yet [then] live publish does
    not queue or clear retention, [else stop]."""

    async def drive() -> tuple[str | None, int, int]:
        hub = WsHub(contract_rev="rev", engine_version="0")
        hub.bind(asyncio.get_running_loop())
        subscriber = Subscriber(
            queue=asyncio.Queue(maxsize=QUEUE_SIZE),
            overflow=asyncio.Event(),
            delivers_shell_quit=True,
            ready=False,
        )
        hub._subscribers.add(subscriber)
        hub.publish(TOPIC_SHELL_QUIT, {"force": True, "source": "lifecycle"})
        await asyncio.sleep(0)
        return (
            hub._pending_shell_quit_body,
            hub.seq,
            subscriber.queue.qsize(),
        )

    pending, seq, queued = asyncio.run(drive())
    assert pending is not None
    assert seq == 0
    assert queued == 0


def test_shell_quit_retained_when_only_browser_subscriber_is_connected() -> None:
    """[if] lifecycle/quit fires while only a browser tab is on /events [then]
    shell.quit stays pending until the shell attaches, [else stop]."""
    hub = WsHub(contract_rev="rev", engine_version="0")
    with TestClient(_app(hub)) as client:
        with client.websocket_connect(EVENTS_PATH) as browser:
            browser.receive_text()
            hub.publish(TOPIC_SHELL_QUIT, {"force": True, "source": "lifecycle"})
            assert hub.seq == 0
            with client.websocket_connect(f"{EVENTS_PATH}?consumer=shell") as shell:
                hello = json.loads(shell.receive_text())
                quit_frame = json.loads(shell.receive_text())

    assert hello["payload"]["seq_start"] == 0
    assert quit_frame["topic"] == TOPIC_SHELL_QUIT
    assert quit_frame["payload"] == {"force": True, "source": "lifecycle"}



def _subscriber(*, shell: bool, ready: bool) -> Subscriber:
    return Subscriber(
        queue=asyncio.Queue(maxsize=QUEUE_SIZE),
        overflow=asyncio.Event(),
        delivers_shell_quit=shell,
        ready=ready,
    )


def test_live_shell_quit_skips_a_shell_that_is_still_connecting() -> None:
    """[if] a live shell.quit fans out while a second shell is mid-hello [then]
    only the ready shell and browsers get the live frame; the connecting shell
    waits for its replay, [else stop]."""

    async def drive() -> tuple[int, int, int]:
        hub = WsHub(contract_rev="rev", engine_version="0")
        hub.bind(asyncio.get_running_loop())
        ready_shell = _subscriber(shell=True, ready=True)
        connecting_shell = _subscriber(shell=True, ready=False)
        browser = _subscriber(shell=False, ready=False)
        for subscriber in (ready_shell, connecting_shell, browser):
            hub._subscribers.add(subscriber)
        hub.publish(TOPIC_SHELL_QUIT, {"force": True, "source": "lifecycle"})
        await asyncio.sleep(0)
        return (
            ready_shell.queue.qsize(),
            connecting_shell.queue.qsize(),
            browser.queue.qsize(),
        )

    ready_count, connecting_count, browser_count = asyncio.run(drive())
    assert ready_count == 1
    assert connecting_count == 0
    assert browser_count == 1


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[1, 2]",
        json.dumps({"topic": "jobs.updated", "seq": 1}),
        json.dumps({"topic": TOPIC_SHELL_QUIT_ACK, "seq": "1"}),
        json.dumps({"topic": TOPIC_SHELL_QUIT_ACK, "seq": -1}),
        json.dumps({"topic": TOPIC_SHELL_QUIT_ACK, "seq": True}),
    ],
)
def test_malformed_shell_ack_raises_instead_of_passing_silently(raw: str) -> None:
    """[if] the shell sends a frame that is not a valid shell.quit.ack [then]
    the hub raises ShellAckProtocolError, [else stop]."""
    hub = WsHub(contract_rev="rev", engine_version="0")
    subscriber = Subscriber(
        queue=cast(Any, None),
        overflow=cast(Any, None),
        delivers_shell_quit=True,
        ready=True,
    )
    with pytest.raises(ShellAckProtocolError):
        hub.handle_shell_quit_ack(subscriber, raw)


def test_malformed_shell_ack_closes_the_shell_socket_with_1008() -> None:
    """[if] a consumer=shell socket sends a malformed ack [then] the endpoint
    closes it with 1008 and retention stays, [else stop]."""
    hub = WsHub(contract_rev="rev", engine_version="0")
    with TestClient(_app(hub)) as client:
        hub.publish(TOPIC_SHELL_QUIT, {"force": True, "source": "lifecycle"})
        with client.websocket_connect(f"{EVENTS_PATH}?consumer=shell") as socket:
            socket.receive_text()
            socket.receive_text()
            socket.send_text("not json")
            with pytest.raises(WebSocketDisconnect) as closed:
                socket.receive_text()
    assert closed.value.code == SHELL_ACK_PROTOCOL_ERROR_CODE
    assert hub._pending_shell_quit_body is not None

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
        self.query_params: dict[str, str] = {}
        self.accepted = False

    async def accept(self) -> None:
        self.accepted = True

    async def send_text(self, text: str) -> None:
        raise RuntimeError('Cannot call "send" once a close message has been sent.')

    async def receive_text(self) -> str:
        raise AssertionError("unreachable: the hello send already failed")

    async def close(self, code: int = 1000, reason: str = "") -> None:
        return None


class _SocketThatDiesBeforeShellQuitReplay:
    """Fails on the shell.quit replay send after the hello landed."""

    def __init__(self, hub: WsHub) -> None:
        self.app = SimpleNamespace(state=SimpleNamespace(event_hub=hub))
        self.query_params = {"consumer": "shell"}
        self.accepted = False
        self._hello_sent = False

    async def accept(self) -> None:
        self.accepted = True

    async def send_text(self, text: str) -> None:
        payload = json.loads(text)
        if payload["topic"] == HELLO_TOPIC:
            self._hello_sent = True
            return
        raise RuntimeError('Cannot call "send" once a close message has been sent.')

    async def receive_text(self) -> str:
        await asyncio.sleep(0)
        raise WebSocketDisconnect(code=1006)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        return None


def test_shell_quit_stays_pending_when_replay_send_fails_before_delivery() -> None:
    """[if] consumer=shell drops after hello but before shell.quit is sent [then]
    a reconnecting shell still receives the quit, [else stop]."""

    async def drive() -> str | None:
        hub = WsHub(contract_rev="rev", engine_version="0")
        hub.bind(asyncio.get_running_loop())
        hub.publish(TOPIC_SHELL_QUIT, {"force": True, "source": "lifecycle"})
        await asyncio.sleep(0)
        socket = _SocketThatDiesBeforeShellQuitReplay(hub)
        await events_endpoint(cast(WebSocket, socket))
        replay = hub.pending_shell_quit_replay_frame()
        return replay

    replay = asyncio.run(drive())
    assert replay is not None
    assert TOPIC_SHELL_QUIT in replay
    assert '"source": "lifecycle"' in replay


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
