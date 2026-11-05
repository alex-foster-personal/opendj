"""The engine's shared booth (B2BBOOTH-16, B2BBOOTH-19, B2BBOOTH-90..96): contract v3 "Authority".

ONE ``Booth`` per engine holds the booth's whole authority for this laptop: the epoch (minted
here when this engine opens as the inviter; a page reload keeps it), the ``BoothCore`` (log,
lease, turn lock, settings), the two lanes to the partner's engine, the timers (grab fallback,
lease renew while this laptop's page is connected, gap re-ask, liveness) and the localhost push
stream to this laptop's page. The page sends intents and renders what it is pushed; it decides
nothing (page-side exceptions, all audio-clock work, are named in the ADR).

Transport is injected (``attach_transport``): ``apps.b2b_booth`` imports no B2B sibling, the web
UI route layer wires the paired door (LAN or internet relay) in. The partner's messages arrive
through ``door_post``, which the door calls for ``POST /b2b-booth/<lane>``.

B2BBOOTH-97: on a LAN pairing the route layer also attaches a sealed datagram channel
(``attach_datagram``, ``apps.b2b_transport.datagram.DatagramSocket``): CTL traffic (controls,
position samples, renews) then rides UDP whenever the datagram probe proves both directions
(``dgram.DatagramLink``), and the door CTL lane otherwise. ``GET /booth`` names the active lane.

B2BBOOTH-99: the page snapshot carries each deck's applied state and this laptop's track resolution
(``resolve_decks_with``); a ``decks`` event follows whenever a resolution changes.

B2BBOOTH-121 (the front door): ``invite_partner`` asks the partner's engine to open its side
(``POST /b2b-booth/invite`` through the same door, injected as ``attach_transport(invite=...)``);
the partner's ``door_post`` hands a valid invite to the handler the route layer registered
(``on_invite``), which wires the transport and opens. One click on either laptop opens both.
"""

from __future__ import annotations

import json
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from typing import Any, Literal, Protocol

from apps.b2b_booth.core import BoothCore, Out
from apps.b2b_booth.dgram import ADVERT_S, DatagramLink, NewestFilter, Repeats, message_key
from apps.b2b_booth.lanes import CtlLane, OpsLane, Post, envelope
from apps.b2b_booth.lease import RENEW_INTERVAL_US
from apps.b2b_booth.protocol import (
    INVITE_PATH,
    LANES,
    PROTOCOL_VERSION,
    SIDES,
    UNITS,
    BoothError,
    Lane,
    Side,
    other_side,
    validate_envelope,
    validate_intent,
    validate_invite,
    validate_op,
    validate_position,
)

BoothStateName = Literal["off", "opening", "live", "degraded"]
Subscriber = Callable[[dict[str, Any]], None]
#: Each deck's track resolution on this laptop (B2BBOOTH-99), from the transfer (``deck -> {state, stable_id, reason}``).
DeckResolver = Callable[[], dict[int, dict[str, Any] | None]]
#: Told (no arguments) after the booth committed or applied an op, restarted or closed; see ``watch_commits``.
CommitWatcher = Callable[[], None]
#: Push-stream event types after which the committed log (or the booth itself) may have changed.
COMMIT_EVENTS = frozenset({"op", "snapshot", "state"})
#: B2BBOOTH-121: one invite POST to the partner (``body``, ``timeout_s``) -> the partner's answer, or BoothError.
Invite = Callable[[dict[str, Any], float], dict[str, Any]]
#: B2BBOOTH-121: this laptop's answer to a partner's invite: ``(side to open, audio_laptop)`` -> booth view.
InviteHandler = Callable[[Side, Side], dict[str, Any]]
#: The partner's engine has this long to open its side (it may wait on the pairing door's connect).
INVITE_TIMEOUT_S = 10.0

#: The inviter's grab fallback is checked this often.
TICK_S = 0.02
#: A tick later than this is counted in ``health.ticks_late`` (the engine process was starved).
TICK_LATE_MS = 50.0
#: A joiner re-asks an open gap (or, with no epoch yet, the whole log) this often.
REASK_S = 0.5
#: No word from the partner for this long turns a live booth ``degraded``.
CONTACT_STALE_S = 1.5
#: A peer POST body is at most this (the door enforces the same cap).
MAX_PEER_BODY = 256 * 1024
#: DELETE waits at most this long for ``end`` to reach the partner before letting go.
END_FLUSH_S = 1.0
#: One datagram's JSON payload stays under this (the sealed frame adds 44 bytes; the transport's cap is 1156).
DGRAM_PAYLOAD_BYTES = 1100
#: Deck resolutions (the transfer's) are read this often, outside the booth lock.
DECKS_POLL_S = 0.25


class DatagramChannel(Protocol):
    """What the booth needs of the sealed datagram socket (``apps.b2b_transport.datagram.DatagramSocket``,
    duck-typed: ``apps.b2b_booth`` imports no transport)."""

    port: int

    @property
    def ready(self) -> bool: ...

    def start(self, on_payload: Callable[[int, bytes, tuple[str, int]], None]) -> None: ...

    def begin(self, epoch: str, side: Side) -> str: ...

    def set_peer(self, epoch: str, port: int, sid: str) -> bool: ...

    def send(self, payload: bytes, addr: tuple[str, int] | None = None) -> bool: ...

    def close(self) -> None: ...

    def status(self) -> dict[str, Any]: ...


def mint_epoch() -> str:
    return secrets.token_hex(8)


@dataclass
class _Timers:
    renew_at: float = 0.0
    reask_at: float = 0.0


class Booth:
    """Everything ``/api/v1/b2b/booth`` reads and does on this engine."""

    def __init__(self, now_us: Callable[[], int] | None = None) -> None:
        if now_us is None:
            from apps.ableton_link.link_clock import link_now_us

            now_us = link_now_us
        self.now_us = now_us
        self._lock = threading.RLock()
        self.core: BoothCore | None = None
        self.side: Side | None = None
        self.closed_epochs = 0
        self._post: Post | None = None
        self._invite: Invite | None = None
        self._invite_handler: InviteHandler | None = None
        self._transport_name: str | None = None
        self._timeout_scale = 1.0
        self._ops: OpsLane | None = None
        self._ctl: CtlLane | None = None
        self._subscribers: list[Subscriber] = []
        self._commit_watchers: list[CommitWatcher] = []
        self._ticker: threading.Thread | None = None
        self._stop = threading.Event()
        self._timers = _Timers()
        self._state: BoothStateName = "off"
        self._ever_live = False
        self._last_contact: float | None = None
        self._last_stale = 0
        self.last_error: str | None = None
        self.counters = {"peer_posts_in": 0, "peer_refused": 0, "transport_errors": 0, "page_intents": 0,
                         "page_positions": 0, "stream_events": 0, "dgram_in": 0, "dgram_out": 0, "dgram_refused": 0,
                         "dgram_unsent": 0}
        self.health: dict[str, float] = {"tick_late_max_ms": 0.0, "ticks_late": 0}
        self._dgram: DatagramChannel | None = None
        self._dgram_epoch: str | None = None
        self._dgram_sid: str | None = None
        self._peer_udp: dict[str, Any] | None = None
        self._advert_at = 0.0
        self.link = DatagramLink()
        self._newest = NewestFilter()
        self._repeats = Repeats()
        self._resolve_decks: DeckResolver | None = None
        self._resolutions: dict[int, dict[str, Any] | None] = {}
        self._decks_at = 0.0

    # ------------------------------------------------------------ wiring

    def attach_transport(self, post: Post | None, name: str | None = None, timeout_scale: float = 1.0,
                         invite: Invite | None = None) -> None:
        """The door POST to the partner (``None`` detaches). Lanes pick it up on the next open;
        ``timeout_scale`` stretches the lanes' POST timeouts for a slower transport; ``invite`` is the
        same door's front-door POST (B2BBOOTH-121)."""
        with self._lock:
            self._post, self._transport_name, self._timeout_scale = post, name, timeout_scale
            self._invite = invite

    def on_invite(self, handler: InviteHandler | None) -> None:
        """B2BBOOTH-121: what opens this laptop's side when the partner presses Start (the route layer)."""
        with self._lock:
            self._invite_handler = handler

    def invite_partner(self, side: Side, audio_laptop: Side) -> dict[str, Any]:
        """B2BBOOTH-121: ask the partner's engine to open the OTHER side, naming the audio laptop.
        Raises BoothError (``no_peer``, or the partner's refusal) rather than returning a guess."""
        with self._lock:
            invite, scale = self._invite, self._timeout_scale
        if invite is None:
            raise BoothError("no_peer", "no paired partner to invite: pair first (LAN or internet)", 409)
        return invite({"v": PROTOCOL_VERSION, "kind": "invite", "from": side, "audio_laptop": audio_laptop},
                      INVITE_TIMEOUT_S * scale)

    def attach_datagram(self, channel: DatagramChannel | None) -> None:
        """The sealed datagram channel to the partner (LAN only; ``None`` detaches and closes the old one).
        Picked up at once: the next tick announces it over the door and starts the probe."""
        with self._lock:
            old, self._dgram = self._dgram, channel
            self._dgram_epoch = self._dgram_sid = None
            self._newest.reset()
            self._repeats.clear()
            self.link.reset(channel is not None)
        if old is not None and old is not channel:
            old.close()
        if channel is not None and channel is not old:
            channel.start(self._datagram_in)

    def resolve_decks_with(self, resolver: DeckResolver | None) -> None:
        """B2BBOOTH-99: where each deck's track resolution comes from (the transfer, wired by the route layer)."""
        with self._lock:
            self._resolve_decks = resolver

    # ------------------------------------------------------------ lifecycle

    def open(self, side: Side) -> dict[str, Any]:
        """Inviter: mint a new epoch. Joiner: no epoch until the inviter's snapshot names it."""
        if side not in SIDES:
            raise BoothError("bad_field", f"side must be inviter or joiner, got {side!r}")
        with self._lock:
            if self._post is None:
                raise BoothError("no_peer", "no paired partner to reach: pair first (LAN or internet)", 409)
            if self.core is not None:
                self.closed_epochs += 1
            self._shutdown_locked()
            now = self.now_us()
            self.side = side
            self.core = BoothCore(side, mint_epoch() if side == "inviter" else None, now)
            self._ever_live, self._last_contact, self._last_stale = False, None, 0
            self._dgram_epoch = self._dgram_sid = self._peer_udp = None
            self._newest.reset()
            self._repeats.clear()
            self.link.reset(self._dgram is not None)
            self._resolutions = {}
            self.health = {"tick_late_max_ms": 0.0, "ticks_late": 0}
            lane_args = (self._lane_post, self.now_us, self._on_answer, self._on_lane_error, self._timeout_scale)
            self._ops = OpsLane(*lane_args)
            self._ctl = CtlLane(*lane_args)
            self._ops.start()
            self._ctl.start()
            if side == "joiner":
                self._ops.put([self.core.hello()])
            self._stop = threading.Event()
            self._ticker = threading.Thread(target=self._tick_loop, args=(self._stop,), name="b2b-booth-tick", daemon=True)
            self._ticker.start()
            self._set_state("opening")
            self._publish_snapshot()
        return self.view(enabled=True)

    def close(self) -> dict[str, Any]:
        """Propose ``end`` (it reaches the partner through the log), then let go here."""
        with self._lock:
            if self.core is None:
                raise BoothError("not_open", "the booth is not open", 409)
            if not self.core.state.ended:
                self._dispatch(self.core.intent_op({"op": "end"}, self.now_us()))
            ops = self._ops
        deadline = monotonic() + END_FLUSH_S
        while ops is not None and ops.status()["pending"] and monotonic() < deadline:
            self._stop.wait(0.02)
        with self._lock:
            self._shutdown_locked()
            self.closed_epochs += 1
            self._set_state("off")
            self._wake_commit_watchers()  # the booth is gone (``end`` may already have set ``off``)
        return self.view(enabled=True)

    def _shutdown_locked(self) -> None:
        self._stop.set()
        for lane in (self._ops, self._ctl):
            if lane is not None:
                lane.stop()
        self._ops = self._ctl = None
        self.core, self.side = None, None

    def shutdown(self) -> None:
        with self._lock:
            self._shutdown_locked()
            channel, self._dgram = self._dgram, None
        if channel is not None:
            channel.close()

    # ------------------------------------------------------------ the page: intents and the stream

    def intent(self, body: Any) -> dict[str, Any]:
        """``POST /booth/intent``: one discrete op or continuous control from this laptop's page or an agent."""
        intent = validate_intent(body)
        with self._lock:
            core = self._require_core()
            now = self.now_us()
            trace = {"engine_us": now} | ({"page_us": intent["t_page_us"]} if "t_page_us" in intent else {})
            if intent["kind"] == "op":
                out = core.intent_op(intent["op"], now, trace)
            else:
                out = core.intent_control(intent["unit"], intent["ctl"], intent["value"], now, trace)
            self.counters["page_intents"] += 1
            self._dispatch(out)
        if out.refused is not None:
            raise BoothError(out.refused, _REFUSAL_MESSAGES.get(out.refused, out.refused), 409)
        return {"accepted": True, "seq": out.seq, "pid": out.pid, "engine_us": now}

    def settings(self, body: dict[str, Any]) -> dict[str, Any]:
        """``PUT /booth/settings``: a ``setting`` op for this laptop's side through the log."""
        with self._lock:
            core = self._require_core()
            op = validate_op({"op": "setting", "side": core.side, **{k: v for k, v in body.items() if v is not None}})
        return self.intent({"kind": "op", "op": op})

    def position(self, body: Any) -> dict[str, Any]:
        """``POST /booth/position``: the audio laptop page's sample, relayed newest-wins per deck."""
        sample = validate_position(body)
        with self._lock:
            core = self._require_core()
            if core.role != "audio":
                raise BoothError("not_audio_laptop", "only the audio laptop's page sends position samples", 409)
            out = core.intent_position(sample)
            self.counters["page_positions"] += 1
            self._dispatch(out)
        return {"accepted": True}

    def subscribe(self, subscriber: Subscriber) -> Callable[[], None]:
        """One page stream. The first event is the snapshot (or ``state off``); every later event follows
        it in order, so a reloaded page starts from the same booth as one that never left."""
        with self._lock:
            subscriber(self._snapshot_event())
            self._subscribers.append(subscriber)

        def unsubscribe() -> None:
            with self._lock:
                if subscriber in self._subscribers:
                    self._subscribers.remove(subscriber)

        return unsubscribe

    def watch_commits(self, watcher: CommitWatcher) -> None:
        """Engine-side consumers of committed ops (B2BBOOTH-40: the track transfer resolves every committed
        ``load``). ``watcher`` is called under the booth lock after any event that may change the log
        (an op committed or applied, a snapshot, a state change incl. ``off``), so it must not block: it
        wakes its own thread, which reads ``log_since``. Unlike a page subscriber it never counts as a
        connected page (the lease renews only for a page)."""
        with self._lock:
            self._commit_watchers.append(watcher)

    @property
    def page_connected(self) -> bool:
        return bool(self._subscribers)

    def _snapshot_event(self) -> dict[str, Any]:
        if self.core is None:
            return {"type": "snapshot", "state": "off", "epoch": None, "side": None, "ops": [], "decks": []}
        snapshot = self.core.page_snapshot()
        return {**snapshot, "decks": self._with_resolution(snapshot["decks"]), "state": self._state}

    def _with_resolution(self, decks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Each deck plus ``resolution``: this laptop's stable_id for its track, or the reason it has none
        yet (``resolving``, ``receiving NN%``, ``not found``); None with no track or no transfer wired."""
        return [{**d, "resolution": self._resolutions.get(d["deck"]) if d["track"] is not None else None} for d in decks]

    def _publish_snapshot(self) -> None:
        self._emit([self._snapshot_event()])

    def _emit(self, events: list[dict[str, Any]]) -> None:
        for raw in events:
            event = {**raw, "state": self._state} if raw["type"] == "snapshot" and "state" not in raw else raw
            self.counters["stream_events"] += 1
            for subscriber in list(self._subscribers):
                subscriber(event)
        if any(e["type"] in COMMIT_EVENTS for e in events):
            self._wake_commit_watchers()

    def _wake_commit_watchers(self) -> None:
        for watcher in list(self._commit_watchers):
            watcher()

    # ------------------------------------------------------------ the partner's engine

    def door_post(self, lane: str, raw: bytes) -> tuple[int, dict[str, Any]]:
        """``POST /b2b-booth/<lane>`` from the partner's engine, through the paired door."""
        if lane == INVITE_PATH:
            return self._door_invite(raw)
        if lane not in LANES:
            return 404, {"detail": {"code": "no_lane", "message": f"booth lanes are ops and ctl, not {lane!r}"}}
        if len(raw) > MAX_PEER_BODY:
            return 413, {"detail": {"code": "too_large", "message": f"a booth envelope is at most {MAX_PEER_BODY} bytes"}}
        try:
            envelope = validate_envelope(json.loads(raw or b"null"), lane)  # type: ignore[arg-type]
            with self._lock:
                core = self._require_core()
                now = self.now_us()
                self.counters["peer_posts_in"] += 1
                self._last_contact = monotonic()
                out = core.receive(envelope, now)
                self._dispatch(out)
                return 200, {"v": PROTOCOL_VERSION, "now_us": self.now_us(), "epoch": core.epoch,
                             "messages": [_strip(m) for m in out.replies], "answers": out.answers}
        except BoothError as exc:
            self.counters["peer_refused"] += 1
            return exc.status, {"detail": {"code": exc.code, "message": str(exc)}}
        except ValueError as exc:
            self.counters["peer_refused"] += 1
            return 400, {"detail": {"code": "bad_json", "message": str(exc)}}

    def _door_invite(self, raw: bytes) -> tuple[int, dict[str, Any]]:
        """B2BBOOTH-121: the partner pressed Start; open this laptop's side through the route layer's handler."""
        with self._lock:
            handler = self._invite_handler
        if handler is None:
            self.counters["peer_refused"] += 1
            return 409, {"detail": {"code": "no_front_door", "message": "this laptop's engine cannot open a booth from an invite"}}
        try:
            sender, audio_laptop = validate_invite(json.loads(raw or b"null"))
            view = handler(other_side(sender), audio_laptop)
        except BoothError as exc:
            self.counters["peer_refused"] += 1
            return exc.status, {"detail": {"code": exc.code, "message": str(exc)}}
        except ValueError as exc:
            self.counters["peer_refused"] += 1
            return 400, {"detail": {"code": "bad_json", "message": str(exc)}}
        return 200, {"v": PROTOCOL_VERSION, "opened": view["side"], "state": view["state"], "epoch": view["epoch"]}

    def _lane_post(self, lane: Lane, envelope: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        post = self._post
        if post is None:
            raise BoothError("no_peer", "the partner transport went away", 409)
        return post(lane, envelope, timeout_s)

    def _on_answer(self, _lane: Lane, answer: dict[str, Any], t0_us: int, t1_us: int) -> None:
        with self._lock:
            core = self.core
            if core is None:
                return
            self._last_contact = monotonic()
            peer_now = answer.get("now_us")
            if isinstance(peer_now, int):
                core.peer_clock.add(t0_us, peer_now, t1_us)
            messages = answer.get("messages") or []
            out = Out()
            if messages:
                try:
                    envelope = validate_envelope({"v": PROTOCOL_VERSION, "epoch": answer.get("epoch"), "sent_us": peer_now,
                                                  "messages": messages}, "ops")
                    out.extend(core.receive(envelope, self.now_us(), reply=True))
                except BoothError as exc:
                    self.last_error = f"bad answer from the partner: {exc.code}: {exc}"
            if answer.get("answers"):
                out.extend(core.answer(answer["answers"]))
            self._dispatch(out)
            self._update_state()

    def _on_lane_error(self, lane: Lane, message: str) -> None:
        with self._lock:
            self.counters["transport_errors"] += 1
            self.last_error = f"{lane} lane: {message}"

    def _dispatch(self, out: Out) -> None:
        """Send what the core produced and push its events to the page (called under the lock)."""
        if self._ops is not None:
            self._ops.put(out.ops)
        if out.ctl:
            if self.link.active == "datagram" and self._dgram is not None and self.core is not None and self.core.epoch:
                self._send_datagrams([m for _, m in out.ctl], self.core.epoch)
                now = monotonic()
                for key, message in out.ctl:
                    self._repeats.schedule(key, message, now)
            elif self._ctl is not None:
                self._ctl.put(out.ctl)
        if out.peer_udp is not None:
            self._peer_udp = out.peer_udp
            self._apply_peer_udp()
        self._emit(out.events)
        if any(e.get("type") == "state" and e.get("state") == "off" for e in out.events):
            self._set_state("off", emit=False)

    def _require_core(self) -> BoothCore:
        if self.core is None:
            raise BoothError("not_open", "the booth is not open on this laptop: POST /api/v1/b2b/booth/open first", 409)
        return self.core

    # ------------------------------------------------------------ time

    def _tick_loop(self, stop: threading.Event) -> None:
        due = monotonic() + TICK_S
        while not stop.wait(max(0.0, due - monotonic())):
            # Engine health: a tick this late means the process was starved (CPU or the GIL) and every
            # push to the page and lane send waited with it (the rig's hop tails need this attribution).
            late_ms = (monotonic() - due) * 1000
            due = max(due + TICK_S, monotonic())
            self.health["tick_late_max_ms"] = max(self.health["tick_late_max_ms"], round(late_ms, 1))
            if late_ms > TICK_LATE_MS:
                self.health["ticks_late"] += 1
            with self._lock:
                if self.core is None or stop.is_set():
                    return
                self._tick_locked()
            if monotonic() >= self._decks_at:
                self._decks_at = monotonic() + DECKS_POLL_S
                self._poll_decks()

    def _tick_locked(self) -> None:
        core = self.core
        assert core is not None
        now, mono = self.now_us(), monotonic()
        out = core.tick(now)
        if mono >= self._timers.renew_at:
            self._timers.renew_at = mono + RENEW_INTERVAL_US / 1e6
            out.extend(core.renew(now) if self.page_connected else _heartbeat(core, now))
        if core.sequencer is None and mono >= self._timers.reask_at:
            self._timers.reask_at = mono + REASK_S
            stale = core.applier.counters.stale_epoch
            if core.epoch is None or stale > self._last_stale:
                self._last_stale = stale
                out.ops.append(core.hello())
            else:
                ask = core.reask_gap()
                if ask is not None:
                    out.ops.append(ask)
        self._dispatch(out)
        self._tick_datagram_locked(core, now, mono)
        self._update_state()

    # ------------------------------------------------------------ the datagram lane (B2BBOOTH-97)

    def _tick_datagram_locked(self, core: BoothCore, now: int, mono: float) -> None:
        channel = self._dgram
        if channel is None or core.epoch is None or self._ctl is None:
            return
        if self._dgram_epoch != core.epoch:
            self._dgram_sid = channel.begin(core.epoch, core.side)
            self._dgram_epoch, self._advert_at = core.epoch, 0.0
            self._newest.reset()
            self._repeats.clear()
            self.link.reset(True)
            self._apply_peer_udp()
        if mono >= self._advert_at:
            # Always over the door: it is how the partner learns where (and under which key) to send.
            self._advert_at = mono + ADVERT_S
            self._ctl.put([("udp", {"kind": "udp", "epoch": core.epoch, "port": channel.port, "sid": self._dgram_sid})])
        if self.link.ping_due(mono, channel.ready):
            self._send_datagrams([{"kind": "ping", "t0_us": now}], core.epoch)
        if self.link.update(mono, channel.ready) == "door":
            # Controls that went out only as datagrams may have been lost: the door gets the newest of each.
            self._repeats.clear()
            self._ctl.put([(f"ctl:{key}", {"kind": "ctl", "epoch": core.epoch, **control})
                           for key, control in core.controls.items() if control["side"] == core.side])
        if self.link.active == "datagram":
            repeats = self._repeats.take(mono)
            if repeats:
                self._send_datagrams([m for _, m in repeats], core.epoch)

    def _apply_peer_udp(self) -> None:
        """The partner's announced datagram socket, once this engine's own session is on the same epoch
        (an announcement can arrive before this engine's tick has begun the epoch: it waits here)."""
        peer = self._peer_udp
        if peer is not None and self._dgram is not None and peer["epoch"] is not None and peer["epoch"] == self._dgram_epoch:
            self._dgram.set_peer(peer["epoch"], peer["port"], peer["sid"])

    def _send_datagrams(self, messages: list[dict[str, Any]], epoch: str) -> None:
        """Pack ``messages`` into as few sealed datagrams as fit; never retransmitted."""
        channel = self._dgram
        assert channel is not None
        batch: list[dict[str, Any]] = []
        for message in messages:
            trial = json.dumps(envelope(epoch, self.now_us(), [*batch, message])).encode()
            if len(trial) <= DGRAM_PAYLOAD_BYTES or not batch:
                batch.append(message)
                continue
            self._send_one_datagram(channel, epoch, batch)
            batch = [message]
        if batch:
            self._send_one_datagram(channel, epoch, batch)

    def _send_one_datagram(self, channel: DatagramChannel, epoch: str, batch: list[dict[str, Any]]) -> None:
        payload = json.dumps(envelope(epoch, self.now_us(), batch)).encode()
        if len(payload) > DGRAM_PAYLOAD_BYTES:
            # One message over a datagram's budget cannot be split: the door carries it.
            if self._ctl is not None:
                self._ctl.put([(message_key(m), m) for m in batch if m["kind"] != "ping"])
            self.counters["dgram_unsent"] += 1
            return
        if channel.send(payload):
            self.counters["dgram_out"] += 1
        else:
            self.counters["dgram_unsent"] += 1

    def _datagram_in(self, counter: int, payload: bytes, addr: tuple[str, int]) -> None:
        """One authenticated datagram from the partner (the channel's receive thread)."""
        try:
            env = validate_envelope(json.loads(payload), "dgram")
        except (BoothError, ValueError):
            with self._lock:
                self.counters["dgram_refused"] += 1
            return
        with self._lock:
            core, channel = self.core, self._dgram
            if core is None or channel is None or env["epoch"] is None or env["epoch"] != core.epoch:
                self.counters["dgram_refused"] += 1
                return
            now, mono = self.now_us(), monotonic()
            self.counters["dgram_in"] += 1
            self._last_contact = mono
            rest: list[dict[str, Any]] = []
            for message in env["messages"]:
                kind = message["kind"]
                if kind == "ping":
                    pong = envelope(core.epoch, now, [{"kind": "pong", "t0_us": message["t0_us"], "now_us": self.now_us()}])
                    channel.send(json.dumps(pong).encode(), addr)
                elif kind == "pong":
                    t1 = self.now_us()
                    if t1 >= message["t0_us"]:
                        core.peer_clock.add(message["t0_us"], message["now_us"], t1)
                        self.link.on_pong(mono, t1 - message["t0_us"])
                elif self._newest.admit(message_key(message), counter):
                    rest.append(message)
            if rest:
                self._dispatch(core.receive({**env, "messages": rest}, now))
            self._update_state()

    def _poll_decks(self) -> None:
        """Read the deck resolutions OUTSIDE the booth lock (the transfer has its own lock), then push a
        ``decks`` event if any changed (B2BBOOTH-99)."""
        resolver = self._resolve_decks
        if resolver is None:
            return
        resolutions = resolver()
        with self._lock:
            if self.core is None or resolutions == self._resolutions:
                return
            self._resolutions = resolutions
            self._emit([{"type": "decks", "decks": self._with_resolution(self.core.decks_view())}])

    def _update_state(self) -> None:
        core = self.core
        if core is None or self._state == "off":
            return
        fresh = self._last_contact is not None and monotonic() - self._last_contact <= CONTACT_STALE_S
        owed = self._ops is not None and self._ops.stuck_s() > CONTACT_STALE_S
        if fresh and not owed and core.epoch is not None and core.peer_clock.offset_us is not None:
            self._ever_live = True
            self._set_state("live")
        elif self._ever_live:
            self._set_state("degraded")

    def _set_state(self, state: BoothStateName, *, emit: bool = True) -> None:
        if self._state == state:
            return
        self._state = state
        if emit:
            self._emit([{"type": "state", "state": state}])

    # ------------------------------------------------------------ reading

    def log_since(self, seq: int) -> dict[str, Any]:
        with self._lock:
            if self.core is None:
                return {"epoch": None, "seq": 0, "ops": []}
            return {"epoch": self.core.epoch, "seq": len(self.core.history), "ops": self.core.history[seq:]}

    def view(self, *, enabled: bool) -> dict[str, Any]:
        with self._lock:
            core = self.core
            if core is None:
                return {"enabled": enabled, "state": "off", "epoch": None, "side": None, "role": None, "seq": 0,
                        "onair": None, "settings": None, "units": [], "decks": [], "counters": dict(self.counters),
                        "ended": False, "closed_epochs": self.closed_epochs, "transport": self._transport_name,
                        "page_connected": self.page_connected, "last_error": self.last_error}
            units = core.state.units_view(core.clock.until)
            return {
                "enabled": enabled,
                "state": self._state,
                "epoch": core.epoch,
                "side": core.side,
                "role": core.role,
                "seq": core.applier.applied_seq,
                "onair": core.state.onair,
                "settings": core.state.settings.as_dict(),
                "units": [{"unit": u, **units[u]} for u in UNITS],
                "decks": [dict(core.state.decks[d]) for d in sorted(core.state.decks)],
                "pending_asks": [] if core.arbiter is None else [
                    {"unit": a.unit, "grabber": a.grabber, "asked_us": a.asked_us} for a in core.arbiter.asks.values()],
                "counters": {**core.counter_values(), **self.counters},
                "lanes": {"ops": self._ops.status() if self._ops else None, "ctl": self._ctl.status() if self._ctl else None,
                          "ctl_active": self.link.active,
                          "datagram": {**self.link.status(monotonic()), "repeats": self._repeats.sent,
                                       "stale_dropped": self._newest.stale,
                                       "channel": self._dgram.status() if self._dgram else None}},
                "peer_clock": {"offset_us": core.peer_clock.offset_us, "rtt_us": core.peer_clock.rtt_us},
                "ended": core.state.ended,
                "closed_epochs": self.closed_epochs,
                "transport": self._transport_name,
                "page_connected": self.page_connected,
                "contact_age_s": None if self._last_contact is None else round(monotonic() - self._last_contact, 3),
                "health": dict(self.health),
                "last_error": self.last_error,
            }


def _heartbeat(core: BoothCore, now_us: int) -> Out:
    """No page connected: renew nothing (leases lapse, per contract), but keep the partner's clock and
    liveness fed with an empty renew."""
    out = Out()
    if core.epoch is not None:
        out.ctl.append(("renew", {"kind": "renew", "epoch": core.epoch, "side": core.side, "units": [], "at_link_us": now_us}))
    return out


def _strip(message: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in message.items() if k != "epoch"}


_REFUSAL_MESSAGES = {
    "not_holder": "this laptop does not hold that unit: grab it first",
    "not_open": "the booth is not open (or the joiner has no epoch from the inviter yet)",
    "ended": "the booth has ended",
    "already_held": "this laptop already holds that unit",
    "already_asked": "a grab of that unit is already waiting for its holder",
    "no_pending_grab": "there is no grab of that unit waiting for this laptop's answer",
    "too_late": "the 1 s for refusing has passed; the grab was granted",
    "not_your_setting": "a DJ sets only their own lock_half_on_turn",
}
