"""B2BRELAY-01..05: carry the Link tempo and beat timeline over the pairing channel when Link cannot.

When the two laptops are paired but Ableton Link cannot see the partner (multicast blocked:
Tailscale, client-isolated Wi-Fi; the ``multicast_blocked`` verdict of
``multicast_diagnosis``), each laptop keeps its OWN local Link session through its own
Carabiner, and this relay keeps the two sessions in step over the B2B door:

- the ON AIR laptop is authoritative for tempo and timeline. Its door answers sealed ``ping``
  (its Link host time, for the clock estimate) and ``state`` (role, tempo, the Link time of
  beat 0, long-polled so a tempo change crosses in about half a round trip);
- the FOLLOWING laptop estimates the offset between the two Link clocks
  (``relay_clock.SkewCorrectedMedian``, slewed), maps the leader's timeline onto its own clock,
  and aligns its LOCAL Link session (tempo, then ``force-beat-at-time`` when the bar phase is
  off by more than ``CFG.DEADBAND_MS``). Its decks follow the local session as always.

One source of truth: the relay only writes to Link when Link itself cannot carry the session.
It engages on ``multicast_blocked``, and before every write it re-checks that this laptop's
Carabiner AND the leader's count 0 Link peers, that both relays are engaged, and that the roles
are FOLLOWING here and ON AIR there. A Link that sees any peer stands the relay down at once.

The wire: every request and answer is AES-GCM sealed under a key derived (HKDF) from the
pairing token, which never crosses the wire itself. A request's associated data names its
path; an answer's binds the request's nonce, so a replayed answer cannot satisfy a new
request. A replayed request only earns an answer the replayer cannot read (both routes are
read-only).

Pure parts (``next_engaged``, ``relay_mode``, ``plan_alignment``, the sealing) are unit
tested; :class:`LinkRelay` holds the threads.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from apps.ableton_link.carabiner import CarabinerClient, CarabinerUnavailable
from apps.ableton_link.link_clock import DEFAULT_QUANTUM, link_now_us
from apps.b2b_sync.relay_clock import ClockEstimate, SkewCorrectedMedian, SlewedOffset, sample_from_ping
from apps.shared.link_constants import LINK

if TYPE_CHECKING:
    # Annotation only: a runtime import reaches b2b_pairing through b2b_transport.channel
    # and breaks the b2b-siblings-independent contract (.importlinter).
    from apps.b2b_transport.channel import PairedChannel

_LOG = logging.getLogger(__name__)


class CFG:
    PING_HZ: float = 4.0  # the page's own clock ping rate, and the spike's
    PING_TIMEOUT_S: float = 1.0
    #: A dead connection is redialled at least this often, so a healed path is found in about this
    #: long rather than after the kernel's SYN backoff (1, 2, 4 s ...).
    CONNECT_TIMEOUT_S: float = 0.5
    #: The leader holds a ``state`` request this long for a change (long poll).
    WATCH_WAIT_S: float = 1.0
    WATCH_TIMEOUT_S: float = 3.0
    #: Reconnect backoff cap after a failed watch: the research's S3 says cap at 1 s for a live set.
    WATCH_BACKOFF_MAX_S: float = 1.0
    #: Long polls kept outstanding at once. With one, a change made while its answer and the next
    #: request are in flight waits up to a full round trip (round 4: 446 ms p95 at 120 ms RTT); with
    #: two, one is nearly always parked at the leader, so a change crosses in about half a round trip.
    WATCHERS: int = 2
    VERDICT_EVERY_S: float = 2.0
    TICK_S: float = 0.25
    #: Bar phase further off than this is forced back (ms). 5x under B2BSLO-02's 5 ms p95.
    DEADBAND_MS: float = 1.0
    #: B2BSLO-02 / link-session LOCKED_MS: within this the follower reads ``locked``.
    LOCKED_MS: float = LINK.locked_ms  # LINKCONST-01
    #: A tempo this close is the same tempo (8 ppm at 128 BPM, below any crystal skew we correct).
    BPM_EPS: float = 0.001
    #: Leader state older than this (a partition, a dead door) and the follower holds.
    STALE_AFTER_S: float = 2.5
    #: The leader's state loop re-reads its Carabiner this often while a long poll waits.
    STATE_POLL_S: float = 0.005
    #: A beat-0 time that moved less than this did not move (Carabiner prints whole us).
    START_CHANGE_US: float = 100.0
    PATH_PREFIX: str = "/b2b-relay/"
    PING_PATH: str = "/b2b-relay/ping"
    STATE_PATH: str = "/b2b-relay/state"
    #: Test rigs only: ``"<offset_s>,<skew_ppm>"`` puts this laptop's relay answers in a shifted,
    #: skewed clock domain, because two engines on one host share one clock. Logged loudly.
    INJECT_ENV: str = "MDT_B2B_RELAY_INJECT_CLOCK"


#: Detector verdicts that engage an idle relay.
ENGAGE_VERDICTS: frozenset[str] = frozenset({"multicast_blocked"})
#: Verdicts an ENGAGED relay holds through: a lost path (ADR-0124: a drop holds the tempo), a
#: probe that could not measure, or a 0-peer streak restarted by a Carabiner reconnect.
HOLD_VERDICTS: frozenset[str] = frozenset({"multicast_blocked", "waiting", "peer_unreachable", "unknown"})

Mode = Literal["idle", "lead", "follow", "clash", "no_on_air"]
Sync = Literal["none", "locking", "locked", "drifting", "holding"]


class RelayWireError(RuntimeError):
    """A relay frame could not be opened (wrong key, tampered, replayed answer, bad shape)."""


# ---------------------------------------------------------------- sealing

_KEY_INFO = b"opendj-b2b relay v1"
_REQUEST_AAD = b"opendj-b2b relay request "
_ANSWER_AAD = b"opendj-b2b relay answer "


def relay_key(token: str) -> bytes:
    if not token:
        raise ValueError("the relay key needs the pairing token")
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_KEY_INFO).derive(token.encode())


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def seal(key: bytes, payload: dict[str, Any], aad: bytes) -> tuple[dict[str, str], bytes]:
    """``({"n", "c"}, nonce)``: the envelope and the nonce an answer must bind."""
    nonce = os.urandom(12)
    sealed = AESGCM(key).encrypt(nonce, json.dumps(payload, separators=(",", ":")).encode(), aad)
    return {"n": _b64(nonce), "c": _b64(sealed)}, nonce


def unseal(key: bytes, envelope: object, aad: bytes) -> tuple[dict[str, Any], bytes]:
    if not isinstance(envelope, dict) or not isinstance(envelope.get("n"), str) or not isinstance(envelope.get("c"), str):
        raise RelayWireError("a relay frame is {n, c} strings")
    try:
        nonce = _unb64(envelope["n"])
        plain = AESGCM(key).decrypt(nonce, _unb64(envelope["c"]), aad)
    except (InvalidTag, ValueError) as exc:
        raise RelayWireError("relay frame failed authentication (wrong pairing, tampered or replayed)") from exc
    payload = json.loads(plain)
    if not isinstance(payload, dict):
        raise RelayWireError("a relay payload is a JSON object")
    return payload, nonce


def request_aad(path: str) -> bytes:
    return _REQUEST_AAD + path.encode()


def answer_aad(request_nonce: bytes) -> bytes:
    return _ANSWER_AAD + request_nonce


# ---------------------------------------------------------------- the wire clock


@dataclass(frozen=True)
class WireClock:
    """What this laptop stamps on the relay wire: its Link time, or (test rigs) a known transform."""

    offset_us: float = 0.0
    skew_ppm: float = 0.0
    ref_us: float = 0.0

    @property
    def injected(self) -> bool:
        return self.offset_us != 0.0 or self.skew_ppm != 0.0

    def stamp(self, local_us: float) -> float:
        return local_us + self.offset_us + (local_us - self.ref_us) * self.skew_ppm * 1e-6

    def tempo(self, bpm: float) -> float:
        """The same physical tempo counted in wire microseconds."""
        return bpm / (1.0 + self.skew_ppm * 1e-6)

    @classmethod
    def from_env(cls, raw: str | None, ref_us: float) -> WireClock:
        if raw is None or raw == "":
            return cls()
        try:
            offset_s, skew_ppm = (float(part) for part in raw.split(","))
        except ValueError as exc:
            raise ValueError(f"{CFG.INJECT_ENV} must be '<offset_s>,<skew_ppm>', got {raw!r}") from exc
        if not (math.isfinite(offset_s) and math.isfinite(skew_ppm)) or abs(skew_ppm) > 1000:
            raise ValueError(f"{CFG.INJECT_ENV} out of range: {raw!r}")
        return cls(offset_us=offset_s * 1e6, skew_ppm=skew_ppm, ref_us=ref_us)


# ---------------------------------------------------------------- pure decisions


def next_engaged(engaged: bool, *, paired: bool, joined: bool, peers: int | None, verdict: str | None) -> tuple[bool, str]:
    """Engage on ``multicast_blocked``; stand down the moment Link sees a peer, or on any verdict
    that says Link is fine or off; hold through a lost path or an unmeasured probe."""
    if not paired:
        return False, "not paired"
    if not joined:
        return False, "Link is not joined on this laptop"
    if peers is not None and peers > 0:
        return False, f"Link sees {peers} peer(s): Link carries tempo and timeline, the relay stands down"
    if verdict in ENGAGE_VERDICTS:
        return True, "the network blocks Link multicast: the relay carries tempo and timeline"
    if engaged and verdict in HOLD_VERDICTS:
        return True, f"holding through '{verdict}' (a lost path holds the tempo)"
    return False, f"Link verdict is '{verdict}', not multicast_blocked"


def relay_mode(engaged: bool, role: str, peer_role: str | None) -> Mode:
    if not engaged:
        return "idle"
    if role == "on_air":
        return "clash" if peer_role == "on_air" else "lead"
    if peer_role == "on_air":
        return "follow"
    return "no_on_air"


@dataclass(frozen=True)
class Timeline:
    bpm: float
    #: Link time (us) of session beat 0, in its own clock.
    start_us: float


@dataclass(frozen=True)
class AlignPlan:
    #: The tempo this laptop's session should run, in its own clock.
    target_bpm: float
    set_bpm: bool
    #: Signed bar-phase error, ms (local minus leader); within half a quantum.
    phase_error_ms: float
    #: ``force-beat-at-time`` this beat at ``at_us``, or None inside the deadband.
    force_beat: float | None
    at_us: float


def _wrap(value: float, quantum: float) -> float:
    """Into [-quantum/2, quantum/2)."""
    return (value + quantum / 2.0) % quantum - quantum / 2.0


def plan_alignment(
    local: Timeline, leader: Timeline, offset_us: float, skew_ppm: float, now_us: float, quantum: float = DEFAULT_QUANTUM
) -> AlignPlan:
    """Map the leader's timeline (in ITS clock: ``offset_us`` = leader time minus ours) onto ours.

    Tempo is converted through the skew (beats per leader us times leader us per our us); the bar
    phase compares the two beats at ``now_us`` modulo the quantum, never raw beat counts."""
    for name, value in (("local bpm", local.bpm), ("leader bpm", leader.bpm)):
        if not (math.isfinite(value) and value > 0):
            raise ValueError(f"{name} must be positive, got {value}")
    target_bpm = leader.bpm * (1.0 + skew_ppm * 1e-6)
    leader_beat = (now_us + offset_us - leader.start_us) * leader.bpm / 60e6
    local_beat = (now_us - local.start_us) * local.bpm / 60e6
    error_beats = _wrap(local_beat - leader_beat, quantum)
    error_ms = error_beats * 60_000.0 / local.bpm
    force = local_beat - error_beats if abs(error_ms) > CFG.DEADBAND_MS else None
    return AlignPlan(
        target_bpm=target_bpm,
        set_bpm=abs(local.bpm - target_bpm) > CFG.BPM_EPS,
        phase_error_ms=error_ms,
        force_beat=force,
        at_us=now_us,
    )


def sync_state(*, mode: Mode, estimate_ready: bool, leader_fresh: bool, phase_error_ms: float | None) -> Sync:
    if mode != "follow":
        return "none"
    if not leader_fresh:
        return "holding"
    if not estimate_ready or phase_error_ms is None:
        return "locking"
    return "locked" if abs(phase_error_ms) <= CFG.LOCKED_MS else "drifting"


# ---------------------------------------------------------------- the service


def _token_of(channel: PairedChannel) -> str:
    """The pairing token the relay's frame key derives from; a channel without one is a bug."""
    if not channel.token:
        raise RelayWireError(f"the {channel.kind} pairing carries no token")
    return channel.token


@dataclass(frozen=True)
class LeaderState:
    """One ``state`` answer, as received (wire clock domain)."""

    seq: int
    role: str
    engaged: bool
    joined: bool
    connected: bool
    peers: int | None
    bpm: float | None
    start_us: float | None
    received_at: float  # local monotonic

    @classmethod
    def parse(cls, payload: dict[str, Any], received_at: float) -> LeaderState:
        try:
            peers = payload["peers"]
            bpm, start = payload["bpm"], payload["start_us"]
            return cls(
                seq=int(payload["seq"]), role=str(payload["role"]), engaged=bool(payload["engaged"]),
                joined=bool(payload["joined"]), connected=bool(payload["connected"]),
                peers=None if peers is None else int(peers), bpm=None if bpm is None else float(bpm),
                start_us=None if start is None else float(start), received_at=received_at,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RelayWireError(f"relay state answer is missing or mistyped: {exc}") from exc


class LinkRelay:
    """Both halves: the door answers (any role) and the follower loops (when engaged)."""

    def __init__(
        self,
        *,
        client: Callable[[], CarabinerClient | None],
        role: Callable[[], str],
        peer: Callable[[], PairedChannel | None],
        verdict: Callable[[], tuple[str, str]],
        enabled: Callable[[], bool],
        wire_clock: WireClock | None = None,
        now_us: Callable[[], float] = link_now_us,
    ) -> None:
        self._client, self._role, self._peer, self._verdict_fn, self._enabled = client, role, peer, verdict, enabled
        self._now_us = now_us
        self.wire = wire_clock if wire_clock is not None else WireClock.from_env(os.environ.get(CFG.INJECT_ENV), now_us())
        if self.wire.injected:
            _LOG.warning("b2b relay: INJECTED wire clock %+.6f s, %+.1f ppm (test rigs only)",
                         self.wire.offset_us / 1e6, self.wire.skew_ppm)
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._state_changed = threading.Event()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.engaged = False
        self.reason = "not started"
        self._verdict: tuple[str, str] | None = None
        self._verdict_at: float | None = None
        self._engaged_since: float | None = None
        self._estimator = SkewCorrectedMedian()
        self._slewed = SlewedOffset()
        self._estimate: ClockEstimate | None = None
        self._leader: LeaderState | None = None
        self._plan: AlignPlan | None = None
        self._last_sync_at: float | None = None
        self._last_error: str | None = None
        self._counts = {"pings": 0, "ping_failures": 0, "watch_answers": 0, "watch_failures": 0,
                        "forces": 0, "tempo_sets": 0, "refused_writes": 0, "engagements": 0}
        # leader half: the state sequence the door serves
        self._state_key: tuple[Any, ...] | None = None
        self._state_seq = 0
        self._carabiner_session_restore: Callable[[], None] | None = None

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        with self._lock:
            if self._threads:
                return
            client = self._client()
            if client is not None:
                self._carabiner_session_restore = client.chain_session_change(self._state_changed.set)
            self._stop.clear()
            self.reason = "starting"
            loops = [("verdict", self._verdict_loop), ("align", self._align_loop), ("ping", self._ping_loop)]
            loops += [(f"watch-{k}", self._watch_loop) for k in range(CFG.WATCHERS)]
            for name, target in loops:
                thread = threading.Thread(target=target, name=f"b2b-relay-{name}", daemon=True)
                self._threads.append(thread)
                thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        threads, self._threads = self._threads, []
        for thread in threads:
            thread.join(timeout=4.0)
        if self._carabiner_session_restore is not None:
            self._carabiner_session_restore()
            self._carabiner_session_restore = None
        with self._lock:
            self.engaged = False
            self.reason = "stopped"

    @property
    def running(self) -> bool:
        return bool(self._threads) and not self._stop.is_set()

    # ------------------------------------------------------------ leader half (door answers)

    def _local_view(self) -> dict[str, Any]:
        client = self._client()
        snap = client.snapshot() if client is not None else None
        bpm = snap.bpm if snap is not None and snap.connected else None
        start = snap.start_us if snap is not None and snap.connected else None
        return {
            "role": self._role(), "engaged": self.engaged, "joined": client is not None,
            "connected": bool(snap and snap.connected), "peers": snap.peers if snap else None,
            "bpm": None if bpm is None else self.wire.tempo(bpm),
            "start_us": None if start is None else self.wire.stamp(start),
        }

    def _current_state(self) -> dict[str, Any]:
        view = self._local_view()
        start = view["start_us"]
        key = (view["role"], view["engaged"], view["joined"], view["connected"], view["peers"],
               None if view["bpm"] is None else round(view["bpm"], 6),
               None if start is None else round(start / CFG.START_CHANGE_US))
        with self._lock:
            if key != self._state_key:
                self._state_key = key
                self._state_seq += 1
                self._state_changed.set()
            return {**view, "seq": self._state_seq}

    def door_post(self, path: str, raw: bytes) -> tuple[int, dict[str, Any]]:
        """Answer a sealed ``ping`` or ``state`` from the partner. (status, JSON body)."""
        door = self._peer()
        if door is None:
            return 409, {"detail": {"code": "not_paired", "message": "this laptop is not paired"}}
        if path not in (CFG.PING_PATH, CFG.STATE_PATH):
            return 404, {"detail": "not a relay path"}
        key = relay_key(_token_of(door))
        try:
            body, nonce = unseal(key, json.loads(raw or b"{}"), request_aad(path))
        except (RelayWireError, ValueError) as exc:
            return 401, {"detail": {"code": "relay_auth", "message": str(exc)}}
        if path == CFG.PING_PATH:
            answer = {"t_us": self.wire.stamp(self._now_us())}
        else:
            since = int(body.get("since", -1))
            wait_s = min(max(float(body.get("wait_s", 0.0)), 0.0), CFG.WATCH_WAIT_S)
            deadline = time.monotonic() + wait_s
            state = self._current_state()
            while state["seq"] == since and time.monotonic() < deadline and not self._stop.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._state_changed.wait(timeout=min(CFG.STATE_POLL_S, remaining))
                self._state_changed.clear()
                state = self._current_state()
            answer = {**state, "t_us": self.wire.stamp(self._now_us())}
        envelope, _ = seal(key, answer, answer_aad(nonce))
        return 200, envelope

    # ------------------------------------------------------------ follower half

    def _exchange(self, http: httpx.Client, door: PairedChannel, path: str, payload: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        """One sealed exchange over the paired channel: the LAN door, or the internet relay room
        (B2BNET-15), whose own Noise session then wraps these already-sealed frames."""
        key = relay_key(_token_of(door))
        envelope, nonce = seal(key, payload, request_aad(path))
        response = door.bind(http).post_sealed(
            path, json.dumps(envelope).encode(), httpx.Timeout(timeout_s, connect=CFG.CONNECT_TIMEOUT_S)
        )
        if response.status_code != 200:
            raise RelayWireError(f"partner answered {path} with {response.status_code}: {response.text[:200]}")
        answer, _ = unseal(key, response.json(), answer_aad(nonce))
        return answer

    def _ping_once(self, http: httpx.Client, door: PairedChannel) -> None:
        key = relay_key(_token_of(door))
        envelope, nonce = seal(key, {}, request_aad(CFG.PING_PATH))
        body = json.dumps(envelope).encode()
        t_send = self._now_us()
        response = door.bind(http).post_sealed(
            CFG.PING_PATH, body, httpx.Timeout(CFG.PING_TIMEOUT_S, connect=CFG.CONNECT_TIMEOUT_S)
        )
        t_recv = self._now_us()
        if response.status_code != 200:
            raise RelayWireError(f"partner answered ping with {response.status_code}: {response.text[:200]}")
        answer, _ = unseal(key, response.json(), answer_aad(nonce))
        sample = sample_from_ping(t_send, float(answer["t_us"]), t_recv)
        with self._lock:
            self._estimator.add(sample)
            self._counts["pings"] += 1

    def _ping_loop(self) -> None:
        http = httpx.Client(timeout=httpx.Timeout(CFG.PING_TIMEOUT_S, connect=CFG.CONNECT_TIMEOUT_S))
        period = 1.0 / CFG.PING_HZ
        try:
            while not self._stop.is_set():
                started = time.monotonic()
                door = self._peer()
                if self.engaged and door is not None:
                    try:
                        self._ping_once(http, door)
                    except (httpx.HTTPError, RelayWireError, ValueError, KeyError) as exc:
                        self._note_failure("ping_failures", f"ping: {type(exc).__name__}: {exc}")
                self._stop.wait(max(0.0, period - (time.monotonic() - started)))
        finally:
            http.close()

    def _watch_loop(self) -> None:
        http = httpx.Client(timeout=httpx.Timeout(CFG.WATCH_TIMEOUT_S, connect=CFG.CONNECT_TIMEOUT_S))
        backoff = 0.1
        #: After a failure the first request asks for the state NOW (since -1), not a long poll that
        #: the leader would hold for WATCH_WAIT_S when nothing changed during the outage.
        fresh_read = True
        try:
            while not self._stop.is_set():
                door = self._peer()
                if not self.engaged or door is None:
                    self._stop.wait(CFG.TICK_S)
                    fresh_read = True
                    continue
                since = self._leader.seq if self._leader is not None and not fresh_read else -1
                try:
                    payload = self._exchange(http, door, CFG.STATE_PATH, {"since": since, "wait_s": CFG.WATCH_WAIT_S},
                                             CFG.WATCH_TIMEOUT_S)
                    state = LeaderState.parse(payload, time.monotonic())
                except (httpx.HTTPError, RelayWireError, ValueError) as exc:
                    self._note_failure("watch_failures", f"state: {type(exc).__name__}: {exc}")
                    self._stop.wait(backoff)
                    backoff = min(CFG.WATCH_BACKOFF_MAX_S, backoff * 2)
                    fresh_read = True
                    continue
                with self._lock:
                    # Two watchers answer the same change; never step back to an older sequence
                    # (after a failure the leader may have restarted, so a fresh read always wins).
                    if fresh_read or self._leader is None or state.seq >= self._leader.seq:
                        self._leader = state
                    self._counts["watch_answers"] += 1
                backoff, fresh_read = 0.1, False
                self._wake.set()
        finally:
            http.close()

    def _note_failure(self, counter: str, message: str) -> None:
        with self._lock:
            self._counts[counter] += 1
            self._last_error = message

    def _verdict_loop(self) -> None:
        while not self._stop.is_set():
            try:
                verdict = self._verdict_fn() if self._enabled() else ("unknown", "link.tempo_sync is off")
            except Exception as exc:  # noqa: BLE001 - any probe failure is an unmeasured verdict, named
                verdict = ("unknown", f"the multicast diagnosis failed: {type(exc).__name__}: {exc}")
            with self._lock:
                self._verdict, self._verdict_at = verdict, time.monotonic()
            self._wake.set()
            self._stop.wait(CFG.VERDICT_EVERY_S)

    def _leader_fresh(self, leader: LeaderState | None) -> bool:
        return leader is not None and time.monotonic() - leader.received_at <= CFG.STALE_AFTER_S

    def _align_loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(CFG.TICK_S)
            self._wake.clear()
            if self._stop.is_set():
                return
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - a tick failure is recorded and retried, never silent
                _LOG.exception("b2b relay tick failed")
                self._note_failure("refused_writes", f"tick: {type(exc).__name__}: {exc}")

    def tick(self) -> None:
        """One supervision step: engagement, then (FOLLOWING an ON AIR leader) one alignment."""
        client = self._client()
        snap = client.snapshot() if client is not None else None
        with self._lock:
            verdict = self._verdict[0] if self._verdict is not None else None
            engaged, reason = next_engaged(
                self.engaged, paired=self._peer() is not None and self._enabled(), joined=client is not None,
                peers=snap.peers if snap is not None and snap.connected else None, verdict=verdict,
            )
            if engaged != self.engaged:
                _LOG.info("b2b relay %s: %s", "ENGAGED" if engaged else "stood down", reason)
                self._engaged_since = time.monotonic() if engaged else None
                if engaged:
                    self._counts["engagements"] += 1
                else:
                    self._leader, self._plan = None, None
            self.engaged, self.reason = engaged, reason
            leader = self._leader
        # The last role heard counts while the partner is silent (a partition holds as "follow");
        # only an alignment needs fresh leader state.
        mode = relay_mode(engaged, self._role(), leader.role if leader is not None else None)
        if mode != "follow" or client is None or snap is None or leader is None or not self._leader_fresh(leader):
            return
        self._align(client, leader)

    def _align(self, client: CarabinerClient, leader: LeaderState) -> None:
        snap = client.snapshot()
        now = self._now_us()
        with self._lock:
            estimate = self._estimator.estimate(now)
            self._estimate = estimate
            if estimate is None or not snap.connected or snap.bpm is None or snap.start_us is None:
                return
            if leader.bpm is None or leader.start_us is None:
                self._last_error = "the ON AIR laptop's Link has no session (Carabiner not reporting)"
                return
            offset = self._slewed.update(estimate.offset_us, now)
        plan = plan_alignment(Timeline(snap.bpm, snap.start_us), Timeline(leader.bpm, leader.start_us),
                              offset, estimate.skew_ppm, now)
        with self._lock:
            self._plan = plan
        if not plan.set_bpm and plan.force_beat is None:
            with self._lock:
                self._last_sync_at = time.monotonic()
            return
        refusal = self._write_refusal(client, leader)
        if refusal is not None:
            self._note_failure("refused_writes", f"write refused: {refusal}")
            return
        try:
            if plan.set_bpm:
                client.command(f"bpm {plan.target_bpm:.6f}")
                with self._lock:
                    self._counts["tempo_sets"] += 1
            if plan.force_beat is not None:
                client.set_beat_at_time(plan.force_beat, int(plan.at_us), DEFAULT_QUANTUM, force=True)
                with self._lock:
                    self._counts["forces"] += 1
        except (CarabinerUnavailable, ValueError) as exc:
            self._note_failure("refused_writes", f"Carabiner refused the alignment: {exc}")
            return
        with self._lock:
            self._last_sync_at = time.monotonic()

    def _write_refusal(self, client: CarabinerClient, leader: LeaderState) -> str | None:
        """The one-source-of-truth guard, re-read right before any Link write. None = allowed."""
        snap = client.snapshot()
        if not self.engaged:
            return "the relay is not engaged"
        if snap.peers is None or snap.peers > 0:
            return f"this laptop's Link counts {snap.peers} peers (Link carries the session)"
        if leader.peers is None or leader.peers > 0:
            return f"the ON AIR laptop's Link counts {leader.peers} peers (Link carries the session)"
        if not leader.engaged:
            return "the ON AIR laptop's relay is not engaged"
        if self._role() != "following" or leader.role != "on_air":
            return f"roles are {self._role()} here and {leader.role} there, not following / on_air"
        return None

    # ------------------------------------------------------------ status

    def relay_peers(self) -> int:
        """1 while engaged with a partner heard within ``CFG.STALE_AFTER_S``, else 0. The page adds it
        to Link's own peer count: the session has a partner, and a silent one is a drop (ADR-0124)."""
        with self._lock:
            return 1 if self.engaged and self._leader_fresh(self._leader) else 0

    def status(self) -> dict[str, Any]:
        now_mono = time.monotonic()
        now_us = self._now_us()
        with self._lock:
            leader, estimate, plan = self._leader, self._estimate, self._plan
            fresh = self._leader_fresh(leader)
            mode = relay_mode(self.engaged, self._role(), leader.role if leader is not None else None)
            if mode == "follow":
                live = self._estimator.estimate(now_us)
                if live is not None:
                    estimate = live
            door = self._peer()
            client = self._client()
            snap = client.snapshot() if client is not None else None
            return {
                "running": self.running,
                #: B2BNET-15: which paired channel carries the relay (``lan`` door or ``internet`` relay room).
                "channel": None if door is None else door.kind,
                "engaged": self.engaged,
                "relay_peers": 1 if self.engaged and fresh else 0,
                "reason": self.reason,
                "engaged_for_s": None if self._engaged_since is None else round(now_mono - self._engaged_since, 2),
                "mode": mode,
                "sync": sync_state(mode=mode, estimate_ready=estimate is not None, leader_fresh=fresh,
                                   phase_error_ms=plan.phase_error_ms if plan is not None else None),
                "verdict": None if self._verdict is None else {
                    "state": self._verdict[0], "reason": self._verdict[1],
                    "age_s": round(now_mono - (self._verdict_at or now_mono), 2),
                },
                "local": {
                    "role": self._role(), "joined": client is not None, "connected": bool(snap and snap.connected),
                    "peers": snap.peers if snap is not None else None, "bpm": snap.bpm if snap is not None else None,
                },
                "peer": None if leader is None else {
                    "url": door.label if door is not None else None, "role": leader.role, "engaged": leader.engaged,
                    "peers": leader.peers, "bpm": leader.bpm, "age_s": round(now_mono - leader.received_at, 2),
                },
                "clock": None if estimate is None else {
                    "offset_ms": round(estimate.offset_us / 1000.0, 3),
                    "applied_offset_ms": None if self._slewed.applied_us is None else round(self._slewed.applied_us / 1000.0, 3),
                    "skew_ppm": round(estimate.skew_ppm, 2),
                    "skew_ready": estimate.skew_ready,
                    "skew_clamped": estimate.skew_clamped,
                    "rtt_ms": round(estimate.rtt_p50_us / 1000.0, 2),
                    "rtt_min_ms": round(estimate.rtt_min_us / 1000.0, 2),
                    "samples": estimate.samples,
                    "steps": self._slewed.steps,
                },
                "phase_error_ms": None if plan is None else round(plan.phase_error_ms, 3),
                "target_bpm": None if plan is None else round(plan.target_bpm, 6),
                "last_sync_age_s": None if self._last_sync_at is None else round(now_mono - self._last_sync_at, 2),
                "counts": dict(self._counts),
                "last_error": self._last_error,
                "injected_clock": None if not self.wire.injected else {
                    "offset_s": self.wire.offset_us / 1e6, "skew_ppm": self.wire.skew_ppm, "ref_us": self.wire.ref_us,
                },
            }
