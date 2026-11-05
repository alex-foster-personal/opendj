"""B2BDEMO-01..05: the B2B demo partner, a scripted "virtual DJ" on this laptop.

A user with nobody to play with presses "Try a demo" (or ``POST /api/v1/b2b/demo``) and
gets a full B2B session against a DEMO peer that lives inside this engine. Nothing here is
a parallel fake UI: the demo serves the SAME two surfaces a real paired laptop serves, so
every real code path runs (ADR: (private path)):

- :class:`DemoPeerDoor`, a loopback HTTP server with the paired door's two read routes,
  ``GET /api/v1/b2b/now-playing`` (built by the real ``shape_now_playing`` from a scripted
  ui-mirror) and ``GET /api/v1/b2b/shouts/outbox`` (a real :class:`ShoutOutbox`). The
  engine's peer config points at it with ``source: "demo"``, so the PEER row, mix strip and
  shout bubbles read it through the normal proxy.
- :class:`VirtualLink`, a loopback TCP server speaking Carabiner's line protocol over a
  virtual Link timeline with ONE peer, the demo DJ. The engine's real ``CarabinerClient``
  dials it, so the Link session store, phase meter, turn button and drop banner run their
  real logic. It is SIMULATED Link: no Ableton Link multicast leaves this laptop, so a live
  Link session on the LAN (a gig in the next room) can never be touched by a demo.

The script (:data:`DEMO_TIMELINE`) is a 135 s B2B: the demo DJ plays, loads its next track,
asks "I'll take the next one?", takes its turn (fader, bass swap, forces the bar), hands
back, drops the connection for 10 s (Link peers 0 and the door answering 503), comes back
and says "That was siiick!". It answers the user's shouts after :data:`REPLY_DELAY_S`.

Every name this peer shows carries "DEMO" (:data:`DEMO_MACHINE`). Nothing here starts on
its own; the engine route is the only caller.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import math
import secrets
import socket
import socketserver
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from apps.ableton_link.link_clock import DEFAULT_QUANTUM, beat_at_time, link_now_us
from apps.b2b_session.b2b_cursor import CursorSlot
from apps.b2b_session.b2b_shout import ShoutOutbox
from apps.b2b_session.on_air import OnAirContext
from apps.b2b_session.peer_now_playing import shape_now_playing

# ----------------------------------------------------------------------------- config
#: The demo DJ's name everywhere a peer name shows (PEER row, bubbles, people list).
DEMO_MACHINE: str = "Demo DJ (DEMO)"
#: Seconds of script; after it the demo keeps playing its last track and reports finished.
DEMO_LENGTH_S: float = 135.0
#: The demo answers each of the user's shouts this long after it arrives.
REPLY_DELAY_S: float = 1.5
#: How the demo answers each preset (anything unlisted gets a thumbs up).
REPLY_FOR_PRESET: dict[str, str] = {"mix_out": "wait", "wait": "up", "take_next": "up", "you_next": "up"}
TICK_S: float = 0.1
CARABINER_VERSION_LINE: str = 'version "1.2.0"'
#: Deck 1 starts this far in (a mid-track drop-in, like a real B2B join), at most this share of the track.
DECK1_START_POSITION_S: float = 60.0
DECK1_START_MAX_SHARE: float = 0.25
MIN_SPEED, MAX_SPEED = 0.25, 20.0


@dataclass(frozen=True)
class DemoCue:
    at_s: float
    kind: str  # start | load | shout | turn | fade | bass_swap | fx | stop_deck1 | drop | back | end
    #: The shout preset id for a shout cue, else the human line.
    detail: str
    #: What the demo card shows for a shout cue (``detail`` is its preset id).
    shown: str | None = None

    @property
    def line(self) -> str:
        return self.shown or self.detail


#: The script. ``detail`` is shown in GET /api/v1/b2b/demo so a viewer (or agent) can follow it.
DEMO_TIMELINE: tuple[DemoCue, ...] = (
    DemoCue(0.0, "start", "Demo DJ joins Link and is ON AIR with deck 1 (mid-track)"),
    DemoCue(8.0, "load", "Demo DJ loads its next track on deck 2"),
    DemoCue(15.0, "shout", "take_next", 'Demo DJ shouts "I\'ll take the next one?"'),
    DemoCue(30.0, "turn", "Demo DJ takes its turn: deck 2 starts on the bar, it forces the Link bar"),
    DemoCue(30.0, "fade", "Deck 2 fader up over 16 s"),
    DemoCue(46.0, "bass_swap", "Bass swap: deck 1 low out, deck 2 low in; deck 1 fades out"),
    DemoCue(56.0, "fx", "Echo on the master bus for 4 s"),
    DemoCue(62.0, "stop_deck1", "Deck 1 stops"),
    DemoCue(72.0, "shout", "you_next", 'Demo DJ hands back: "You take the next one"'),
    DemoCue(90.0, "drop", "Connection drop: Link peers 0 and the peer channel down for 10 s"),
    DemoCue(100.0, "back", "Connection back"),
    DemoCue(115.0, "shout", "sick", 'Demo DJ shouts "That was siiick!"'),
    DemoCue(DEMO_LENGTH_S, "end", "Demo over: the demo DJ keeps playing until you exit"),
)
DROP_S, BACK_S = 90.0, 100.0
TURN_S, FADE_END_S, BASS_SWAP_S, FADE_OUT_END_S, STOP_DECK1_S, LOAD_S = 30.0, 46.0, 46.0, 60.0, 62.0, 8.0
FX_ON_S, FX_OFF_S = 56.0, 60.0
FADER_UP: float = 0.85


@dataclass(frozen=True)
class DemoTrack:
    """A library track the demo DJ plays (metadata only, read from this engine's library)."""

    stable_id: str
    title: str
    artist: str | None
    key: str | None
    bpm: float
    duration_s: float
    content_hash: str | None = None
    overview: dict[str, Any] | None = None
    overview_error: str | None = None


# ----------------------------------------------------------------------------- the script (pure)


def _ramp(t: float, t0: float, t1: float, v0: float, v1: float) -> float:
    if t <= t0:
        return v0
    if t >= t1:
        return v1
    return v0 + (v1 - v0) * (t - t0) / (t1 - t0)


def demo_mix_at(t: float) -> dict[str, Any]:
    """The demo DJ's mixer at script time ``t``: per deck fader/EQ/filter/playing, plus
    master deck and FX. Pure, so the script is unit-tested without threads."""
    deck1_playing = t < STOP_DECK1_S
    deck2_loaded = t >= LOAD_S
    deck2_playing = t >= TURN_S
    return {
        "1": {
            "loaded": True,
            "playing": deck1_playing,
            "fader": _ramp(t, BASS_SWAP_S, FADE_OUT_END_S, FADER_UP, 0.0),
            "eq_low": 0.5 if t < BASS_SWAP_S else 0.0,
            "filter": _ramp(t, 50.0, FADE_OUT_END_S, 0.5, 0.22) if t < STOP_DECK1_S else 0.5,
        },
        "2": {
            "loaded": deck2_loaded,
            "playing": deck2_playing,
            "fader": _ramp(t, TURN_S, FADE_END_S, 0.0, FADER_UP),
            "eq_low": 0.0 if t < BASS_SWAP_S else 0.5,
            "filter": 0.5,
        },
        "master_deck": 1 if t < BASS_SWAP_S else 2,
        "fx_on": FX_ON_S <= t < FX_OFF_S,
        "link_up": not (DROP_S <= t < BACK_S),
    }


def demo_mirror(t: float, tracks: tuple[DemoTrack, DemoTrack], positions_s: tuple[float, float], session_bpm: float) -> dict[str, Any]:
    """The ui-mirror document a real performance page would publish at script time ``t``.
    The real ``shape_now_playing`` turns it into the peer answer."""
    mix = demo_mix_at(t)
    decks: dict[str, Any] = {}
    channels: dict[str, Any] = {}
    for index, deck_id in enumerate(("1", "2")):
        state, track = mix[deck_id], tracks[index]
        if not state["loaded"]:
            decks[deck_id] = {"stable_id": None}
            continue
        position_s = positions_s[index]
        decks[deck_id] = {
            "stable_id": track.stable_id, "content_hash": track.content_hash, "title": track.title,
            "artist": track.artist, "key": track.key, "bpm": track.bpm, "effective_bpm": session_bpm,
            "duration_ms": track.duration_s * 1000.0, "position": {"ms": position_s * 1000.0},
            "beat_at_position": position_s * track.bpm / 60.0, "playing": state["playing"],
            "level_peak": round(state["fader"] * 0.72, 3) if state["playing"] else 0.0,
        }
        channels[deck_id] = {"eq_low": state["eq_low"], "eq_mid": 0.5, "eq_high": 0.5, "filter": round(state["filter"], 3),
                             "fader": round(state["fader"], 3), "assign": "THRU", "stem_eq_mode": False,
                             # LINKAIR-01: THRU, so the gain the mixer lets through is the fader.
                             "on_air_gain": round(state["fader"], 3)}
    return {
        "decks": decks, "master_deck": mix["master_deck"],
        "mixer": {"crossfader": 0.5, "channels": channels},
        "beat_fx": {"available": True, "effect": "Echo", "enabled": mix["fx_on"], "beats": 1.0, "level": 0.6},
    }


def demo_cue_at(t: float) -> DemoCue:
    """The last cue at or before ``t`` (what the demo is doing now)."""
    current = DEMO_TIMELINE[0]
    for cue in DEMO_TIMELINE:
        if cue.at_s <= t:
            current = cue
    return current


def demo_next_cue(t: float) -> DemoCue | None:
    return next((cue for cue in DEMO_TIMELINE if cue.at_s > t), None)


# ----------------------------------------------------------------------------- virtual Link


class VirtualLink:
    """A Carabiner line-protocol server on 127.0.0.1 over a virtual Link session with one
    peer (the demo DJ). ``status``, ``bpm``, ``request-beat-at-time``,
    ``force-beat-at-time`` and ``version`` answer as Carabiner 1.2.0 does; every change is
    pushed to every connection, as Carabiner pushes. Nothing touches the network."""

    def __init__(self, bpm: float) -> None:
        self._lock = threading.Lock()
        self._bpm = float(bpm)
        self._start_us = float(link_now_us())
        self.peer_visible = True
        self._conns: set[socket.socket] = set()
        link = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                with link._lock:
                    link._conns.add(self.connection)
                try:
                    for raw in self.rfile:
                        reply = link.answer(raw.decode("ascii", errors="replace").strip())
                        if reply is not None:
                            link._send(self.connection, reply)
                except OSError:
                    pass
                finally:
                    with link._lock:
                        link._conns.discard(self.connection)

        self._server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, name="b2b-demo-link", daemon=True)
        self._thread.start()

    # -- session state
    @property
    def bpm(self) -> float:
        with self._lock:
            return self._bpm

    def beat_now(self) -> float:
        with self._lock:
            return beat_at_time(self._start_us, self._bpm, link_now_us())

    def status_line(self) -> str:
        with self._lock:
            now = link_now_us()
            beat = beat_at_time(self._start_us, self._bpm, now)
            peers = 1 if self.peer_visible else 0
            return f"status {{ :peers {peers} :bpm {self._bpm:.6f} :start {int(self._start_us)} :beat {beat:.6f} }}"

    def set_bpm(self, bpm: float) -> None:
        """Any peer's tempo proposal wins (Link); the beat at now is kept, as Link keeps it."""
        with self._lock:
            now = link_now_us()
            beat = beat_at_time(self._start_us, self._bpm, now)
            self._bpm = float(bpm)
            self._start_us = now - beat * 60_000_000.0 / self._bpm

    def force_beat_at_time(self, beat: float, at_us: float) -> None:
        with self._lock:
            self._start_us = at_us - beat * 60_000_000.0 / self._bpm

    def request_beat_at_time(self, beat: float, at_us: float, quantum: float) -> None:
        """With a peer, Link keeps the session phase and only renumbers whole bars; alone it
        maps the beat outright (Link's ``requestBeatAtTime``)."""
        with self._lock:
            if not self.peer_visible:
                self._start_us = at_us - beat * 60_000_000.0 / self._bpm
                return
            current = beat_at_time(self._start_us, self._bpm, at_us)
            bars = round((beat - current) / quantum)
            self._start_us -= bars * quantum * 60_000_000.0 / self._bpm

    def set_peer_visible(self, visible: bool) -> None:
        with self._lock:
            changed = self.peer_visible != visible
            self.peer_visible = visible
        if changed:
            self.push()

    # -- protocol
    def answer(self, line: str) -> str | None:
        parts = line.split()
        if not parts:
            return None
        verb, args = parts[0], parts[1:]
        try:
            if verb == "status":
                return self.status_line()
            if verb == "version":
                return CARABINER_VERSION_LINE
            if verb == "bpm":
                bpm = float(args[0])
                if not (math.isfinite(bpm) and 20.0 <= bpm <= 999.0):
                    return f"bad-bpm {args[0]}"
                self.set_bpm(bpm)
                self.push()
                return None
            if verb in ("force-beat-at-time", "request-beat-at-time"):
                beat, at_us, quantum = float(args[0]), float(args[1]), float(args[2])
                if verb == "force-beat-at-time":
                    self.force_beat_at_time(beat, at_us)
                elif verb == "request-beat-at-time":
                    self.request_beat_at_time(beat, at_us, quantum)
                return self.status_line()
        except (IndexError, ValueError):
            return f"bad-{verb} {' '.join(args)}"
        return f"unsupported {verb}"

    def push(self) -> None:
        line = self.status_line()
        with self._lock:
            conns = list(self._conns)
        for conn in conns:
            self._send(conn, line)

    def _send(self, conn: socket.socket, line: str) -> None:
        try:
            conn.sendall((line + "\n").encode("ascii"))
        except OSError:
            with self._lock:
                self._conns.discard(conn)

    def stop(self) -> None:
        self._server.shutdown()
        with self._lock:
            conns = list(self._conns)
            self._conns.clear()
        for conn in conns:
            with contextlib.suppress(OSError):
                conn.shutdown(socket.SHUT_RDWR)
        self._server.server_close()
        self._thread.join(timeout=2)


# ----------------------------------------------------------------------------- peer door


NOW_PLAYING_ROUTE = "/api/v1/b2b/now-playing"
OUTBOX_ROUTE = "/api/v1/b2b/shouts/outbox"
#: B2BCURSOR-01: the demo DJ has no mouse, so its slot stays empty (seq 0, cursor null); without the
#: route the peer cursor overlay would read the demo as a broken contract (502 peer_error).
CURSOR_ROUTE = "/api/v1/b2b/cursor/latest"


class DemoPeerDoor:
    """The demo DJ's door: the two GETs a paired laptop's door serves, bearer-token guarded,
    on 127.0.0.1 only. ``down`` answers 503 for every route (the scripted drop)."""

    def __init__(self, now_playing: Callable[[bool], dict[str, Any]], outbox: ShoutOutbox, token: str) -> None:
        self.down = False
        cursor_slot = CursorSlot(machine=outbox.machine)
        expected = f"Bearer {token}".encode()
        door = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "opendj-b2b-demo/1"

            def _send(self, status: int, body: dict[str, Any]) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                parts = urlsplit(self.path)
                if parts.path not in (NOW_PLAYING_ROUTE, OUTBOX_ROUTE, CURSOR_ROUTE):
                    self._send(404, {"detail": "not served by the demo peer"})
                    return
                if not hmac.compare_digest((self.headers.get("Authorization") or "").encode(), expected):
                    self._send(401, {"detail": "wrong demo token"})
                    return
                if door.down:
                    self._send(503, {"detail": "DEMO: simulated connection drop"})
                    return
                if parts.path == CURSOR_ROUTE:
                    self._send(200, cursor_slot.latest())
                    return
                params = parse_qs(parts.query)
                if parts.path == NOW_PLAYING_ROUTE:
                    self._send(200, now_playing(params.get("with_overview", ["0"])[0] == "1"))
                    return
                since = params.get("since", ["0"])[0]
                if not since.isdigit():
                    self._send(422, {"detail": "since must be a non-negative integer"})
                    return
                self._send(200, outbox.since(int(since)))

            def do_POST(self) -> None:
                self._send(405, {"detail": "read-only"})

            def log_message(self, format: str, *args: object) -> None:  # noqa: ARG002  # quiet: polled at 3 Hz
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, name="b2b-demo-door", daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)


# ----------------------------------------------------------------------------- the session


@dataclass
class DemoSession:
    """One running demo: the virtual Link, the door, and the script runner thread.

    ``user_outbox`` is THIS engine's own shout outbox (what a real peer would pull over the
    door); the demo DJ answers each new shout in it after :data:`REPLY_DELAY_S`."""

    tracks: tuple[DemoTrack, DemoTrack]
    user_outbox: ShoutOutbox
    speed: float = 1.0
    start_at_s: float = 0.0
    token: str = field(default_factory=lambda: secrets.token_urlsafe(18))
    outbox: ShoutOutbox = field(default_factory=lambda: ShoutOutbox(machine=DEMO_MACHINE))
    link: VirtualLink | None = None
    door: DemoPeerDoor | None = None
    started_wall_ms: int = 0
    script_s: float = 0.0
    positions_s: list[float] = field(default_factory=lambda: [0.0, 0.0])
    events: list[dict[str, Any]] = field(default_factory=list)
    replied: dict[str, float] = field(default_factory=dict)
    _fired: set[int] = field(default_factory=set)
    _user_seq: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None

    def __post_init__(self) -> None:
        if not (MIN_SPEED <= self.speed <= MAX_SPEED):
            raise ValueError(f"demo speed must be {MIN_SPEED}..{MAX_SPEED}, got {self.speed}")
        if not (0.0 <= self.start_at_s <= DEMO_LENGTH_S):
            raise ValueError(f"demo start_at_s must be 0..{DEMO_LENGTH_S:g}, got {self.start_at_s}")

    # -- lifecycle
    def start(self) -> None:
        self.positions_s[0] = min(DECK1_START_POSITION_S, self.tracks[0].duration_s * DECK1_START_MAX_SHARE)
        self.link = VirtualLink(round(self.tracks[0].bpm, 2))
        self.door = DemoPeerDoor(self.now_playing, self.outbox, self.token)
        self.started_wall_ms = int(time.time() * 1000)
        self._user_seq = self.user_outbox.since(0)["seq"]  # only shouts sent from now on get answers
        with self._lock:
            self._advance_to(self.start_at_s, catch_up=True)
        self._thread = threading.Thread(target=self._run, name="b2b-demo-script", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self.door is not None:
            self.door.stop()
        if self.link is not None:
            self.link.stop()

    def _run(self) -> None:
        last = time.monotonic()
        while not self._stop.wait(TICK_S):
            now = time.monotonic()
            try:
                with self._lock:
                    self._advance_to(self.script_s + (now - last) * self.speed, catch_up=False)
                self._answer_user_shouts(now)
            except Exception as exc:  # noqa: BLE001  # a dead runner freezes the demo silently; record it in status + stderr
                self._log("error", f"{type(exc).__name__}: {exc}")
                sys.stderr.write(f"b2b_demo runner error: {exc!r}\n")
            last = now

    # -- script
    def _advance_to(self, t: float, catch_up: bool) -> None:
        """Move the script to ``t``, integrating playheads at the session tempo and firing
        each cue once. ``catch_up`` (a start mid-script) applies state without shouting."""
        assert self.link is not None and self.door is not None
        dt = max(0.0, t - self.script_s)
        session_bpm = self.link.bpm
        mix = demo_mix_at(self.script_s)
        for index, deck_id in enumerate(("1", "2")):
            if mix[deck_id]["playing"]:
                rate = session_bpm / self.tracks[index].bpm
                # A track shorter than the script loops (deck loop), so the demo never shows a dead deck.
                self.positions_s[index] = (self.positions_s[index] + dt * rate) % self.tracks[index].duration_s
        if catch_up and t >= TURN_S:
            self.positions_s[1] = ((t - TURN_S) * session_bpm / self.tracks[1].bpm) % self.tracks[1].duration_s
        self.script_s = t
        for index, cue in enumerate(DEMO_TIMELINE):
            if cue.at_s <= t and index not in self._fired:
                self._fired.add(index)
                if not catch_up or cue.at_s == t:
                    self._fire(cue)
        link_up = demo_mix_at(t)["link_up"]
        self.door.down = not link_up
        self.link.set_peer_visible(link_up)

    def _fire(self, cue: DemoCue) -> None:
        assert self.link is not None
        if cue.kind == "shout":
            self.outbox.shout(cue.detail, None)
        elif cue.kind == "turn":
            # The demo DJ goes ON AIR the way Open DJ does: session bar 1 = its deck's bar 1.
            self.positions_s[1] = 0.0
            self.link.force_beat_at_time(0.0, float(link_now_us()))
            self.link.push()
        self._log(cue.kind, cue.line)

    def _answer_user_shouts(self, now: float) -> None:
        page = self.user_outbox.since(self._user_seq)
        self._user_seq = page["seq"]
        for event in page["events"]:
            if event.get("kind") == "shout" and isinstance(event.get("id"), str):
                self.replied.setdefault(event["id"], now + REPLY_DELAY_S)
        for shout_id, due in list(self.replied.items()):
            if due is not None and now >= due:
                shout = self.user_outbox.own_shout(shout_id)
                reply = REPLY_FOR_PRESET.get(str(shout.get("preset_id")) if shout else "", "up")
                self.outbox.reply(shout_id, reply)
                self.replied[shout_id] = math.inf  # answered; never again
                self._log("reply", f"answered your shout {shout_id} with {reply}")

    def _log(self, kind: str, detail: str) -> None:
        self.events.append({"at_s": round(self.script_s, 2), "kind": kind, "detail": detail})
        del self.events[:-50]

    # -- answers
    def now_playing(self, with_overview: bool) -> dict[str, Any]:
        """The door's now-playing answer, through the real shaper (mirror age 0: always fresh)."""
        assert self.link is not None
        with self._lock:
            mirror = demo_mirror(self.script_s, self.tracks, (self.positions_s[0], self.positions_s[1]), self.link.bpm)
        answer = shape_now_playing(mirror, 0.0, DEMO_MACHINE, 3.0, OnAirContext.demo())
        by_id = {track.stable_id: track for track in self.tracks}
        for deck in answer["decks"]:
            track = by_id[deck["stable_id"]]
            deck["overview"] = track.overview if with_overview else None
            deck["overview_error"] = track.overview_error if with_overview else None
            deck["overview_hash"] = None if track.overview is None else track.overview.get("hash")
        answer["demo"] = True
        return answer

    def status(self) -> dict[str, Any]:
        assert self.link is not None and self.door is not None
        with self._lock:
            t = self.script_s
            cue, nxt = demo_cue_at(t), demo_next_cue(t)
            return {
                "active": True,
                "machine": DEMO_MACHINE,
                "link": "simulated",
                "link_note": "DEMO Link is simulated on this laptop: no Ableton Link traffic leaves it.",
                "speed": self.speed,
                "script_s": round(t, 2),
                "length_s": DEMO_LENGTH_S,
                "finished": t >= DEMO_LENGTH_S,
                "now": {"kind": cue.kind, "detail": cue.line},
                "next": None if nxt is None else {"at_s": nxt.at_s, "kind": nxt.kind, "detail": nxt.line},
                "dropped": self.door.down,
                "session_bpm": round(self.link.bpm, 3),
                "peer_url": self.door.url,
                "link_port": self.link.port,
                "tracks": [{"stable_id": tr.stable_id, "title": tr.title, "artist": tr.artist, "bpm": tr.bpm} for tr in self.tracks],
                "events": list(self.events),
                "summary": None if t < DEMO_LENGTH_S else demo_summary(self.events),
            }


def demo_summary(events: list[dict[str, Any]]) -> str:
    shouts = sum(1 for e in events if e["kind"] == "shout")
    replies = sum(1 for e in events if e["kind"] == "reply")
    return (f"Demo over: the demo DJ played 2 tracks, took one turn and handed back, sent {shouts} shouts, "
            f"answered {replies} of yours, and dropped the connection once. Exit the demo to go back to your own setup.")


__all__ = [
    "DEFAULT_QUANTUM", "DEMO_LENGTH_S", "DEMO_MACHINE", "DEMO_TIMELINE", "DemoPeerDoor", "DemoSession",
    "DemoTrack", "VirtualLink", "demo_cue_at", "demo_mirror", "demo_mix_at", "demo_summary",
]
