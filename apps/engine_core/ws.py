"""WS event hub -- the live implementation behind ``events.set_hub()``.

The socket is an INVALIDATION BUS, not a replay log. ``seq`` is a single
engine-lifetime counter across all topics, so a client that sees a gap knows
it missed something and refetches over HTTP. There is no persistence and no
resume cursor: a documented non-goal, not an omission.

Backpressure is the interesting part. Each connection gets a bounded queue;
when it overflows, the socket is CLOSED with 1013 'slow consumer' and the
client reconnects and refetches. Silent drops would leave a client
confidently stale, and unbounded buffering would trade a visible disconnect
for an invisible memory leak.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from starlette.websockets import WebSocket, WebSocketDisconnect

# Per-connection send queue. 256 frames is roughly a second of a very busy
# engine; a client that cannot keep up with that is not going to catch up.
QUEUE_SIZE: int = 256
SLOW_CONSUMER_CODE: int = 1013
SLOW_CONSUMER_REASON: str = "slow consumer"

HELLO_TOPIC: str = "hello"
TOPIC_JOBS_UPDATED: str = "jobs.updated"
TOPIC_HEALTH_CHANGED: str = "health.changed"
TOPIC_LIBRARY_CHANGED: str = "library.changed"
TOPICS: tuple[str, ...] = (
    TOPIC_JOBS_UPDATED,
    TOPIC_HEALTH_CHANGED,
    TOPIC_LIBRARY_CHANGED,
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


@dataclass(eq=False)
class Subscriber:
    """One connected socket's outbox.

    ``eq=False`` keeps identity hashing: subscribers live in a set, and two
    connections with equal-looking queues are still two different sockets.
    """

    queue: asyncio.Queue[str]
    overflow: asyncio.Event


class WsHub:
    """Implements the ``events.EventHub`` protocol: publish(topic, payload)."""

    def __init__(
        self,
        *,
        contract_rev: str,
        engine_version: str,
        queue_size: int = QUEUE_SIZE,
    ) -> None:
        self.contract_rev = contract_rev
        self.engine_version = engine_version
        self.queue_size = queue_size
        self._subscribers: set[Subscriber] = set()
        self._seq: int = 0
        self._loop: asyncio.AbstractEventLoop | None = None

    # ----- lifecycle -----------------------------------------------------
    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def unbind(self) -> None:
        self._loop = None

    @property
    def seq(self) -> int:
        return self._seq

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    # ----- publish -------------------------------------------------------
    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        """Fan out one event. Safe to call from any thread.

        The payload is serialized HERE, in the caller's thread, so a
        non-serializable payload raises at the emit point where someone can
        act on it, instead of vanishing inside a loop callback.
        """
        loop = self._loop
        if loop is None:
            raise RuntimeError(
                "WsHub.publish() before bind(): the hub was registered "
                "without a running engine loop"
            )
        body = json.dumps(payload, sort_keys=True)
        loop.call_soon_threadsafe(self._dispatch, topic, body)

    def _dispatch(self, topic: str, body: str) -> None:
        """Runs on the engine loop, so seq assignment and delivery order
        agree for every subscriber."""
        self._seq += 1
        frame = _envelope(topic, self._seq, body)
        for subscriber in self._subscribers:
            if subscriber.overflow.is_set():
                continue
            try:
                subscriber.queue.put_nowait(frame)
            except asyncio.QueueFull:
                subscriber.overflow.set()

    # ----- subscription --------------------------------------------------
    def attach(self) -> tuple[Subscriber, str]:
        """Register a socket and mint its hello frame in one atomic step.

        Both halves are synchronous and the loop is single-threaded, so no
        event can slip between the subscribe and the seq_start snapshot.
        """
        subscriber = Subscriber(
            queue=asyncio.Queue(maxsize=self.queue_size),
            overflow=asyncio.Event(),
        )
        self._subscribers.add(subscriber)
        hello = _envelope(
            HELLO_TOPIC,
            self._seq,
            json.dumps(
                {
                    "contract_rev": self.contract_rev,
                    "engine_version": self.engine_version,
                    "seq_start": self._seq,
                    "topics": list(TOPICS),
                },
                sort_keys=True,
            ),
        )
        return subscriber, hello

    def detach(self, subscriber: Subscriber) -> None:
        self._subscribers.discard(subscriber)


def _envelope(topic: str, seq: int, body: str) -> str:
    # Assembled as text because ``body`` is already encoded: re-parsing it
    # just to re-dump it would double the cost of every fan-out.
    return (
        f'{{"topic":{json.dumps(topic)},"seq":{seq},'
        f'"ts":{json.dumps(_now())},"payload":{body}}}'
    )


async def events_endpoint(websocket: WebSocket) -> None:
    """``/api/v1/events``. Hello frame first, then the bus."""
    hub: WsHub | None = getattr(websocket.app.state, "event_hub", None)
    if hub is None:
        await websocket.close(code=1011, reason="engine event hub not mounted")
        return
    await websocket.accept()
    subscriber, hello = hub.attach()
    await websocket.send_text(hello)
    # Only the send side runs as a task. The disconnect is awaited in this
    # coroutine directly, so noticing a closed socket costs one scheduler hop
    # rather than several -- an ASGI server that cancels the connection task
    # right after delivering the disconnect must still find us finished.
    pump = asyncio.create_task(_pump(websocket, subscriber))
    try:
        while True:
            # The bus is one-way; reading exists only to notice the client
            # left, so whatever it sends is deliberately discarded.
            await websocket.receive_text()
    except WebSocketDisconnect:
        return
    finally:
        pump.cancel()
        hub.detach(subscriber)


async def _pump(websocket: WebSocket, subscriber: Subscriber) -> None:
    try:
        while not subscriber.overflow.is_set():
            frame = await subscriber.queue.get()
            await websocket.send_text(frame)
        await websocket.close(
            code=SLOW_CONSUMER_CODE, reason=SLOW_CONSUMER_REASON
        )
    except WebSocketDisconnect:
        # The client vanished mid-send; the endpoint body is already on its
        # way to the same conclusion.
        return


__all__ = [
    "HELLO_TOPIC",
    "QUEUE_SIZE",
    "SLOW_CONSUMER_CODE",
    "SLOW_CONSUMER_REASON",
    "TOPICS",
    "TOPIC_HEALTH_CHANGED",
    "TOPIC_JOBS_UPDATED",
    "Subscriber",
    "WsHub",
    "events_endpoint",
]
