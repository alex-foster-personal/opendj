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
from typing import Any

from starlette.websockets import WebSocket, WebSocketDisconnect

from apps.engine_core.utc_timestamps import utc_iso_ms

# Per-connection send queue. 256 frames is roughly a second of a very busy
# engine; a client that cannot keep up with that is not going to catch up.
QUEUE_SIZE: int = 256
SLOW_CONSUMER_CODE: int = 1013
SLOW_CONSUMER_REASON: str = "slow consumer"

HELLO_TOPIC: str = "hello"
TOPIC_JOBS_UPDATED: str = "jobs.updated"
TOPIC_HEALTH_CHANGED: str = "health.changed"
TOPIC_LIBRARY_CHANGED: str = "library.changed"
TOPIC_SHELL_QUIT: str = "shell.quit"
# Client-to-server only (not bus fan-out). Clears retained shell.quit state.
TOPIC_SHELL_QUIT_ACK: str = "shell.quit.ack"
TOPIC_ANALYSIS_CHANGED: str = "analysis.changed"
TOPICS: tuple[str, ...] = (
    TOPIC_JOBS_UPDATED,
    TOPIC_HEALTH_CHANGED,
    TOPIC_LIBRARY_CHANGED,
    TOPIC_ANALYSIS_CHANGED,
)

# Every ``kind`` a ``library.changed`` payload may carry (INFRA-8304). The
# browser's allowlist (``LIBRARY_KINDS`` in
# apps/webui/frontend/src/lib/api/events-bus.ts) logs any other kind as
# contract drift, so a new publish site adds its kind here AND there;
# tests/engine_core/test_library_changed_kinds.py fails when either side or a
# publish site drifts.
LIBRARY_CHANGED_KINDS: tuple[str, ...] = (
    "tracks",
    "playlists",
    "mytags",
    "hot_cues",
    "transition_in",
    "beatgrid_edit",
    "smartlists",
    "dedup",
    "reconcile",
    "ui_prefs",
    "pairings",
    "library_jobs",
    "playlist_sets",
    "playlist_components",
    "midi_maps",
    "set_notes",
    "sections",
    "plays",
    "cloudsync_policy",
    "cloudsync_playlist_pin",
    "tags",
    "works",
    "set_target",
    "undo",
    "source_flags",
    "edits",
)


# RFC 6455 1008 (policy violation): the shell broke the ack wire contract.
SHELL_ACK_PROTOCOL_ERROR_CODE: int = 1008


class ShellAckProtocolError(ValueError):
    """A ``consumer=shell`` socket sent a frame that is not a valid ack."""


@dataclass(eq=False)
class Subscriber:
    """One connected socket's outbox.

    ``eq=False`` keeps identity hashing: subscribers live in a set, and two
    connections with equal-looking queues are still two different sockets.

    ``delivers_shell_quit`` marks the desktop-shell webview (``consumer=shell``
    on attach). ``shell.quit`` is retained until one such subscriber is
    connected and receives the frame; browser tabs on the same bus do not
    count (issue #6262).
    """

    queue: asyncio.Queue[str]
    overflow: asyncio.Event
    delivers_shell_quit: bool = False
    # True only after the hello frame (and any shell.quit replay) reached the
    # socket. Until then _dispatch must not treat this subscriber as able to
    # receive shell.quit (issue #6262).
    ready: bool = False


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
        # shell.quit is a one-shot lifecycle signal (OPS-07). publish() with no
        # WS subscriber used to burn a seq and drop the frame; the desktop shell
        # could POST lifecycle/quit before /events attaches (issue #6262).
        self._pending_shell_quit_body: str | None = None
        # Seq of the last emitted retained shell.quit (live queue or replay).
        # Cleared only when the shell acks that exact seq (issue #6262).
        self._pending_shell_quit_seq: int | None = None
        # True when a publish() scheduled _dispatch before any ready shell
        # subscriber existed; replay owns seq minting, so a late _dispatch must
        # not fan out the same retention again (issue #6262).
        self._shell_quit_stale_dispatch_pending: bool = False

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

    def _has_shell_quit_subscriber(self) -> bool:
        return any(
            subscriber.delivers_shell_quit and subscriber.ready
            for subscriber in self._subscribers
        )

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
        # allow_nan=False is the whole point. Python's default emits the bare
        # tokens NaN/Infinity, which are not JSON: the client's JSON.parse
        # throws, the frame reads as malformed, and every status transition
        # costs a full resync forever. Better to fail at the emit point,
        # naming the topic, than to ship a frame nobody can parse.
        try:
            body = json.dumps(payload, sort_keys=True, allow_nan=False)
        except ValueError as exc:
            raise ValueError(
                f"WsHub.publish({topic!r}): payload is not serializable as "
                f"strict JSON ({exc}); no frame was emitted"
            ) from exc
        loop.call_soon_threadsafe(self._dispatch, topic, body)

    def _dispatch(self, topic: str, body: str) -> None:
        """Runs on the engine loop, so seq assignment and delivery order
        agree for every subscriber."""
        if topic == TOPIC_SHELL_QUIT:
            self._pending_shell_quit_body = body
            if not self._has_shell_quit_subscriber():
                self._pending_shell_quit_seq = None
                self._shell_quit_stale_dispatch_pending = True
                return
            if self._shell_quit_stale_dispatch_pending:
                self._shell_quit_stale_dispatch_pending = False
                return
        self._seq += 1
        if topic == TOPIC_SHELL_QUIT:
            self._pending_shell_quit_seq = self._seq
        frame = _envelope(topic, self._seq, body)
        for subscriber in self._subscribers:
            if (
                topic == TOPIC_SHELL_QUIT
                and subscriber.delivers_shell_quit
                and not subscriber.ready
            ):
                # A connecting shell gets the retained quit as its replay once
                # hello is out; queuing the live frame too would hand it the
                # same quit twice. Browser subscribers still get the frame:
                # seq is one counter across topics, so skipping them would
                # read as a gap and force a full resync (they ignore it).
                continue
            self.enqueue_frame(subscriber, frame)

    def enqueue_frame(self, subscriber: Subscriber, frame: str) -> None:
        """Queue ``frame``; a full outbox marks the subscriber as overflowed."""
        if subscriber.overflow.is_set():
            return
        try:
            subscriber.queue.put_nowait(frame)
        except asyncio.QueueFull:
            subscriber.overflow.set()

    # ----- subscription --------------------------------------------------
    def attach(self, *, delivers_shell_quit: bool = False) -> tuple[Subscriber, str]:
        """Register a socket and mint its hello frame in one atomic step.

        Both halves are synchronous and the loop is single-threaded, so no
        event can slip between the subscribe and the seq_start snapshot.
        """
        subscriber = Subscriber(
            queue=asyncio.Queue(maxsize=self.queue_size),
            overflow=asyncio.Event(),
            delivers_shell_quit=delivers_shell_quit,
        )
        self._subscribers.add(subscriber)
        hello_seq = self._seq
        hello = _envelope(
            HELLO_TOPIC,
            hello_seq,
            json.dumps(
                {
                    "contract_rev": self.contract_rev,
                    "engine_version": self.engine_version,
                    "seq_start": hello_seq,
                    "topics": list(TOPICS),
                },
                sort_keys=True,
            ),
        )
        return subscriber, hello

    def pending_shell_quit_replay_frame(self) -> str | None:
        """Mint one replay envelope for ``_pending_shell_quit_body``, if any."""
        if self._pending_shell_quit_body is None:
            return None
        self._seq += 1
        self._pending_shell_quit_seq = self._seq
        return _envelope(
            TOPIC_SHELL_QUIT, self._seq, self._pending_shell_quit_body
        )

    def handle_shell_quit_ack(self, subscriber: Subscriber, raw: str) -> None:
        """Drop retained shell.quit state once the desktop shell received it.

        Raises ``ShellAckProtocolError`` on a frame that is not a well-formed
        ``shell.quit.ack``: the shell sends nothing else on this socket, so a
        malformed frame is a broken client and must not pass silently. A
        well-formed ack that is stale or early is ignored by design (A2).
        """
        try:
            message = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ShellAckProtocolError(f"shell sent a non-JSON frame: {exc}") from exc
        if not isinstance(message, dict):
            raise ShellAckProtocolError("shell sent a non-object frame")
        if message.get("topic") != TOPIC_SHELL_QUIT_ACK:
            raise ShellAckProtocolError(
                f"shell sent topic {message.get('topic')!r}, "
                f"expected {TOPIC_SHELL_QUIT_ACK!r}"
            )
        seq = message.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            raise ShellAckProtocolError(
                f"shell ack seq {seq!r} is not a non-negative integer"
            )
        if not subscriber.delivers_shell_quit or not subscriber.ready:
            return
        if self._pending_shell_quit_body is None:
            return
        if seq != self._pending_shell_quit_seq:
            return
        self._pending_shell_quit_body = None
        self._pending_shell_quit_seq = None

    def detach(self, subscriber: Subscriber) -> None:
        self._subscribers.discard(subscriber)


def _envelope(topic: str, seq: int, body: str) -> str:
    # Assembled as text because ``body`` is already encoded: re-parsing it
    # just to re-dump it would double the cost of every fan-out.
    return (
        f'{{"topic":{json.dumps(topic)},"seq":{seq},'
        f'"ts":{json.dumps(utc_iso_ms())},"payload":{body}}}'
    )


async def events_endpoint(websocket: WebSocket) -> None:
    """``/api/v1/events``. Hello frame first, then the bus."""
    hub: WsHub | None = getattr(websocket.app.state, "event_hub", None)
    if hub is None:
        await websocket.close(code=1011, reason="engine event hub not mounted")
        return
    await websocket.accept()
    # attach() is pure and synchronous, so nothing can interleave between it
    # and the try below. Everything that can FAIL -- starting with the hello
    # send -- lives inside, because a client that dies before the hello lands
    # would otherwise leave its subscriber in the hub forever, holding a
    # 256-frame queue that nothing ever drains.
    delivers_shell_quit = websocket.query_params.get("consumer") == "shell"
    subscriber, hello = hub.attach(delivers_shell_quit=delivers_shell_quit)
    pump: asyncio.Task[None] | None = None
    try:
        await websocket.send_text(hello)
        if delivers_shell_quit:
            # Queued, not sent directly, so the pump delivers any frame published
            # during the hello send first and sequence numbers stay ordered.
            replay_shell_quit = hub.pending_shell_quit_replay_frame()
            if replay_shell_quit is not None:
                hub.enqueue_frame(subscriber, replay_shell_quit)
        # Only the send side runs as a task. The disconnect is awaited in this
        # coroutine directly, so noticing a closed socket costs one scheduler
        # hop rather than several -- an ASGI server that cancels the connection
        # task right after delivering the disconnect must still find us
        # finished.
        if delivers_shell_quit and hub._pending_shell_quit_body is not None:
            # Retained quit replays through the pump; ready only after that frame
            # is sent so ack seq matches delivery and live fan-out cannot race hello.
            pump = asyncio.create_task(
                _pump(websocket, subscriber, mark_shell_ready_after_send=True)
            )
        else:
            if delivers_shell_quit:
                subscriber.ready = True
            pump = asyncio.create_task(_pump(websocket, subscriber))
        while True:
            # The bus is one-way for engine events; shell clients may ack
            # shell.quit receipt so retention clears only after delivery.
            raw = await websocket.receive_text()
            if delivers_shell_quit:
                try:
                    hub.handle_shell_quit_ack(subscriber, raw)
                except ShellAckProtocolError as exc:
                    await websocket.close(
                        code=SHELL_ACK_PROTOCOL_ERROR_CODE, reason=str(exc)[:120]
                    )
                    return
    except WebSocketDisconnect:
        return
    finally:
        if pump is not None:
            pump.cancel()
        hub.detach(subscriber)


async def _pump(
    websocket: WebSocket,
    subscriber: Subscriber,
    *,
    mark_shell_ready_after_send: bool = False,
) -> None:
    try:
        while not subscriber.overflow.is_set():
            frame = await subscriber.queue.get()
            await websocket.send_text(frame)
            if mark_shell_ready_after_send and not subscriber.ready:
                subscriber.ready = True
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
    "TOPIC_LIBRARY_CHANGED",
    "TOPIC_SHELL_QUIT",
    "TOPIC_SHELL_QUIT_ACK",
    "Subscriber",
    "WsHub",
    "events_endpoint",
]
