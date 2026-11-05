"""B2BNET-01..04: the internet adapter. Pair by code through the relay, then a sealed door channel.

ADR-0145-b2b-internet-collab, "Connectivity". The relay (``cloud--b2b-relay/``) is a Cloudflare
Durable Object rendezvous plus a dumb pipe. This module is its only client:

1. **Pairing** (``InternetInvite`` / ``join_over_relay``): the inviter asks the relay for a
   public nameplate and shows ``<nameplate>-K7Q-4MX``. The joiner finds the room by nameplate
   only; the 6 characters go into the same SPAKE2 exchange the LAN uses (``b2b_pairing``) and
   never reach the relay. The relay caps SPAKE2 attempts per nameplate at 5, and so does the
   inviter's own ``Invite``. The long-lived token and a random 256-bit room id travel sealed.
2. **Session** (``DoorChannel``): at every connect, a Noise ``NNpsk0`` handshake keyed by the
   token (``noise``) gives fresh per-direction keys. Door messages (``door``) ride as sealed
   frames with explicit counters and a replay window. The relay sees sizes and timing only.
   A door message bigger than one relay frame (64 KiB) is split across sealed fragments and
   reassembled; one over ``MAX_DOOR_MESSAGE_BYTES`` is refused locally with 413 (B2BNET-08).

Configuration is explicit: the relay URL comes from ``MDT_B2B_RELAY_URL`` and there is no
default; anything that needs it fails with ``relay_unconfigured`` when it is unset.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import struct
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

import httpx
from websockets.exceptions import ConnectionClosed, InvalidStatus, InvalidURI
from websockets.sync.client import ClientConnection, connect

from apps.b2b_pairing.b2b_pairing import Invite, JoinerHandshake, PairingError, normalize_code
from apps.b2b_transport.door import DoorAnswer, DoorRequest
from apps.b2b_transport.noise import Initiator, NoiseError, Responder, Session, derive_psk, prologue_for
from apps.shared.private_files import restrict_to_owner

RELAY_URL_ENV = "MDT_B2B_RELAY_URL"
INTERNET_PAIRING_FILE = "b2b-internet-pairing.json"
PROTOCOL_VERSION = 1
#: Seconds the joiner waits for each inviter answer during pairing.
PAIRING_LEG_TIMEOUT_S = 15.0
CONNECT_TIMEOUT_S = 10.0
#: Joiner re-sends handshake message 1 this often while a peer is present but no session exists.
HANDSHAKE_RETRY_S = 2.0
RECONNECT_BACKOFF_S = (0.5, 1.0, 2.0, 5.0)
#: Room frames: 1-byte kind, then the Noise message or ``counter(8) || ciphertext``.
FRAME_HANDSHAKE_1 = 1
FRAME_HANDSHAKE_2 = 2
FRAME_DATA = 3
_COUNTER = struct.Struct(">Q")
#: B2BNET-08: one door message (its JSON) is at most this; a larger one is refused locally with 413.
MAX_DOOR_MESSAGE_BYTES = 8 * 1024 * 1024
#: Plaintext per data frame. Sealed (+16 tag) behind kind(1) and counter(8), it stays under the
#: relay's 64 KiB room-frame cap ((private path) MAX_ROOM_FRAME).
DOOR_FRAGMENT_BYTES = 60_000
#: First plaintext byte of every data frame: more fragments follow, or this one ends the message.
FRAGMENT_MORE, FRAGMENT_LAST = 1, 0
#: The ``closed`` reason the relay's Rendezvous alarm sends when an invite's time-box ends (B2BNET-06/10).
RELAY_EXPIRED_REASON = "invite expired"

Role = Literal["inviter", "joiner"]


# ---------------------------------------------------------------- configuration


def relay_url() -> str:
    """The relay base URL from config, or a named refusal. No default, by design."""
    raw = os.environ.get(RELAY_URL_ENV, "").strip()
    if not raw:
        raise PairingError("relay_unconfigured", f"Internet B2B needs a relay: set {RELAY_URL_ENV} to its URL", 503)
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise PairingError("relay_unconfigured", f"{RELAY_URL_ENV} must be an http(s) URL, got {raw!r}", 503)
    return raw.rstrip("/")


def _ws_url(base: str, path: str) -> str:
    parts = urlsplit(base)
    scheme = "wss" if parts.scheme == "https" else "ws"
    return urlunsplit((scheme, parts.netloc, parts.path.rstrip("/") + path, "", ""))


def _open_socket(base: str, path: str) -> ClientConnection:
    """A relay WebSocket, with the relay's named HTTP refusal turned into a ``PairingError``."""
    try:
        return connect(_ws_url(base, path), open_timeout=CONNECT_TIMEOUT_S, max_size=2**17,
                       compression=None, user_agent_header="opendj-b2b-internet/1")
    except InvalidStatus as exc:
        status = exc.response.status_code
        try:
            error = json.loads(exc.response.body or b"{}").get("error", {})
        except ValueError:
            error = {}
        code = str(error.get("code") or f"relay_http_{status}")
        raise PairingError(_RELAY_CODES.get(code, code), str(error.get("message") or f"relay refused ({status})"),
                           _RELAY_STATUS.get(code, 502)) from exc
    except (OSError, InvalidURI, TimeoutError) as exc:
        raise PairingError("relay_unreachable", f"cannot reach the B2B relay: {type(exc).__name__}: {exc}", 502) from exc


#: Relay refusal codes -> the names the LAN pairing already uses, where one fits.
_RELAY_CODES = {"nameplate_closed": "invite_closed", "attempts_exhausted": "invite_closed", "inviter_absent": "no_inviter_found"}
_RELAY_STATUS = {"nameplate_closed": 410, "attempts_exhausted": 410, "inviter_absent": 404, "busy": 409,
                 "upgrade_rate_limited": 429, "room_budget_exceeded": 429, "relay_internal": 503}


# ---------------------------------------------------------------- the code shown with a nameplate


def format_internet_code(nameplate: str, code: str) -> str:
    return f"{nameplate}-{code}"


def parse_internet_code(typed: str) -> tuple[str, str]:
    """``12-K7Q-4MX`` -> (``12``, ``K7Q4MX``). The nameplate is digits; the rest is the LAN code."""
    head, sep, rest = typed.strip().partition("-")
    if not sep or not head.isdigit() or not 1 <= int(head) <= 9999:
        raise PairingError("bad_code_format", "An internet invite code is a number then 6 letters or digits, like 12-K7Q-4MX", 422)
    return str(int(head)), normalize_code(rest)


# ---------------------------------------------------------------- persisted pairing


@dataclass(frozen=True)
class InternetPairing:
    peer_device_id: str
    peer_label: str
    token: str
    room: str
    role: Role
    paired_at: str


def load_internet_pairing(path: Path) -> InternetPairing | None:
    if not path.exists():
        return None
    return InternetPairing(**json.loads(path.read_text()))


def save_internet_pairing(path: Path, pairing: InternetPairing) -> None:
    """Atomic, owner-only (0600): the file holds the shared token."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    restrict_to_owner(tmp)  # 0o600 alone sets no owner-only ACL on Windows
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(asdict(pairing), indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _send_json(ws: ClientConnection, body: dict[str, Any]) -> None:
    ws.send(json.dumps(body))


def _recv_json(ws: ClientConnection, timeout: float | None) -> dict[str, Any]:
    raw = ws.recv(timeout=timeout)
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise PairingError("bad_message", "relay frame is not a JSON object")
    return data


# ---------------------------------------------------------------- inviter half of pairing


InviteState = Literal["open", "paired", "cancelled", "burned", "expired", "relay_lost"]


class InternetInvite:
    """One open internet invite: a relay nameplate, a LAN-style ``Invite``, one serving thread."""

    def __init__(self, base: str, device_id: str, label: str, on_paired: Callable[[InternetPairing], None]) -> None:
        try:
            response = httpx.post(f"{base}/v1/nameplates", timeout=CONNECT_TIMEOUT_S)
        except httpx.HTTPError as exc:
            raise PairingError("relay_unreachable", f"cannot reach the B2B relay: {type(exc).__name__}: {exc}", 502) from exc
        if response.status_code == 429:
            error = response.json().get("error", {})
            raise PairingError(str(error.get("code") or "rate_limited"), str(error.get("message") or response.text), 429)
        if response.status_code != 201:
            raise PairingError("relay_refused", f"relay would not open a nameplate ({response.status_code}): {response.text}", 502)
        allocation = response.json()
        self.nameplate = str(allocation["nameplate"])
        claim = str(allocation["claim"])
        # B2BNET-10: the relay's own deadline for this invite, relative so neither clock matters.
        expires_in_ms = allocation.get("expires_in_ms")
        if not isinstance(expires_in_ms, int | float) or isinstance(expires_in_ms, bool) or expires_in_ms <= 0:
            refusal = PairingError("relay_refused", f"relay gave no invite deadline (expires_in_ms={expires_in_ms!r})", 502)
            _release_nameplate(base, self.nameplate, claim, refusal)
            raise refusal
        self.expires_at = datetime.now(UTC) + timedelta(milliseconds=float(expires_in_ms))
        self._invite = Invite.create(device_id, label)
        self._on_paired = on_paired
        self._room = secrets.token_urlsafe(32)
        self.state: InviteState = "open"
        self.error: str | None = None
        try:
            self._ws = _open_socket(base, f"/v1/nameplates/{self.nameplate}/ws?side=inviter&claim={claim}")
        except PairingError as exc:
            _release_nameplate(base, self.nameplate, claim, exc)
            raise
        self._thread = threading.Thread(target=self._serve, name=f"b2b-internet-invite-{self.nameplate}", daemon=True)
        self._thread.start()

    @property
    def code(self) -> str:
        return format_internet_code(self.nameplate, self._invite.code)

    @property
    def attempts_left(self) -> int:
        return self._invite.attempts_left

    def is_open(self) -> bool:
        return self.state == "open" and self._invite.is_open()

    def status(self) -> dict[str, Any]:
        left = (self.expires_at - datetime.now(UTC)).total_seconds()
        return {"code": self.code, "open": self.is_open(), "attempts_left": self.attempts_left, "state": self.state,
                "error": self.error, "expires_at": self.expires_at.isoformat(timespec="seconds"),
                "expires_in_s": max(0, int(left))}

    def cancel(self) -> None:
        if self.state == "open":
            self.state = "cancelled"
            with contextlib.suppress(ConnectionClosed, OSError):
                _send_json(self._ws, {"t": "release"})
        self._ws.close()
        self._thread.join(timeout=5)

    def _serve(self) -> None:
        try:
            while self.state == "open":
                self._answer(_recv_json(self._ws, timeout=None))
        except ConnectionClosed:
            if self.state == "open":
                self.state, self.error = "relay_lost", "the relay closed the invite"
        except (PairingError, ValueError, KeyError) as exc:
            self.state, self.error = "relay_lost", f"bad frame from the relay: {exc}"
            self._ws.close()

    def _finish(self, frame: dict[str, Any]) -> None:
        sealed = self._invite.finish(str(frame["session"]), str(frame["confirm"]), extra={"room": self._room})
        paired = self._invite.paired
        assert paired is not None, "finish returned without pairing"
        _send_json(self._ws, {"t": "sealed", **sealed})
        _send_json(self._ws, {"t": "release"})
        self.state = "paired"
        self._on_paired(InternetPairing(peer_device_id="", peer_label="", token=paired.token, room=self._room,
                                        role="inviter", paired_at=_now()))

    def _answer(self, frame: dict[str, Any]) -> None:
        kind = frame.get("t")
        if kind == "closed":
            reason = str(frame.get("reason"))
            if not self._invite.is_open():
                self.state = "burned"
            elif reason == RELAY_EXPIRED_REASON:
                self.state = "expired"
            else:
                self.state = "relay_lost"
            self.error = reason
            return
        if kind not in ("pake", "confirm"):
            raise PairingError("bad_message", f"unexpected rendezvous frame {kind!r}")
        try:
            if kind == "pake":
                leg = self._invite.start(str(frame["msg"]), {"door_port": 0}, "internet-relay")
                _send_json(self._ws, {"t": "pake", "session": leg["session"], "msg": leg["msg_b"], "confirm": leg["confirm_b"]})
            elif kind == "confirm":
                self._finish(frame)
        except PairingError as exc:
            _send_json(self._ws, {"t": "error", "code": exc.code, "message": str(exc), "attempts_left": self.attempts_left})
            if not self._invite.is_open():
                self.state = "burned"
                _send_json(self._ws, {"t": "release"})


def _release_nameplate(base: str, nameplate: str, claim: str, cause: PairingError) -> None:
    """B2BNET-06: give back a nameplate whose inviter socket never opened. When the relay cannot be
    told, ``cause`` says so (its unclaimed TTL frees the slot anyway) rather than hiding it."""
    try:
        response = httpx.delete(f"{base}/v1/nameplates/{nameplate}", params={"claim": claim}, timeout=CONNECT_TIMEOUT_S)
    except httpx.HTTPError as exc:
        cause.add_note(f"nameplate {nameplate} not released ({type(exc).__name__}: {exc}); the relay frees it on its TTL")
        return
    if response.status_code not in (204, 410):
        cause.add_note(f"nameplate {nameplate} not released (relay answered {response.status_code}); it frees it on its TTL")


# ---------------------------------------------------------------- joiner half of pairing


def join_over_relay(base: str, typed_code: str) -> InternetPairing:
    """Type the inviter's ``12-K7Q-4MX``; SPAKE2 through the relay; the sealed token and room come back."""
    nameplate, code = parse_internet_code(typed_code)
    handshake = JoinerHandshake(code)
    ws = _open_socket(base, f"/v1/nameplates/{nameplate}/ws?side=joiner")
    try:
        _send_json(ws, {"t": "pake", "msg": handshake.message()})
        answer = _expect(ws, "pake")
        confirm_a = handshake.check_inviter(str(answer["msg"]), str(answer["confirm"]))
        _send_json(ws, {"t": "confirm", "session": answer["session"], "confirm": confirm_a})
        sealed = _expect(ws, "sealed")
        outcome = handshake.open_sealed({"nonce": str(sealed["nonce"]), "sealed": str(sealed["sealed"])})
    finally:
        ws.close()
    room = outcome.extra.get("room", "")
    if len(room) != 43:
        raise PairingError("bad_seal", "the inviter sealed no internet room id", 502)
    return InternetPairing(peer_device_id=outcome.inviter_device_id, peer_label=outcome.inviter_label,
                           token=outcome.token, room=room, role="joiner", paired_at=_now())


def _expect(ws: ClientConnection, kind: str) -> dict[str, Any]:
    try:
        frame = _recv_json(ws, timeout=PAIRING_LEG_TIMEOUT_S)
    except TimeoutError as exc:
        raise PairingError("peer_unreachable", "the inviting laptop did not answer through the relay", 504) from exc
    except ConnectionClosed as exc:
        raise PairingError("invite_closed", "the relay closed this invite (used, cancelled or too many wrong codes)", 410) from exc
    if frame.get("t") == "error":
        code = str(frame.get("code"))
        raise PairingError(code, str(frame.get("message") or code), {"wrong_code": 403, "attempts_exhausted": 410}.get(code, 409))
    if frame.get("t") == "closed":
        raise PairingError("invite_closed", f"the relay closed this invite: {frame.get('reason')}", 410)
    if frame.get("t") != kind:
        raise PairingError("bad_message", f"expected {kind!r} from the inviter, got {frame.get('t')!r}")
    return frame


# ---------------------------------------------------------------- the sealed door channel


Server = Callable[[DoorRequest], DoorAnswer]


@dataclass(frozen=True)
class Hello:
    device_id: str
    label: str


class DoorChannel:
    """The post-pairing room: Noise NNpsk0 per connect, then door requests both ways.

    The joiner is always the Noise initiator; the inviter answers. A new responder session is
    held PENDING until a data frame authenticates under it, so a replayed handshake message 1
    cannot knock out the live session.
    """

    def __init__(self, base: str, pairing: InternetPairing, serve: Server, hello: Hello,
                 on_hello: Callable[[Hello], None], record_wire: bool = False) -> None:
        self.base = base
        self.pairing = pairing
        self._serve = serve
        self._hello = hello
        self._on_hello = on_hello
        self._psk = derive_psk(pairing.token)
        self._prologue = prologue_for(pairing.room)
        self._lock = threading.RLock()
        #: Held across every fragment of one message so two messages never interleave on the wire.
        self._send_lock = threading.RLock()
        self._inbound = bytearray()
        #: The counter the next data frame must carry; a gap means the relay dropped or reordered frames.
        self._inbound_next: int | None = None
        #: After a gap, frames are dropped up to the next message boundary so no partial message is spliced.
        self._inbound_resync = False
        self._ws: ClientConnection | None = None
        self._session: Session | None = None
        self._pending: Session | None = None
        self._initiator: Initiator | None = None
        self._peer_present = False
        self._established = threading.Event()
        self._stopping = threading.Event()
        self._next_id = 0
        self._waiters: dict[int, tuple[threading.Event, list[DoorAnswer]]] = {}
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="b2b-internet-serve")
        self.peer: Hello | None = None
        self.last_error: str | None = None
        #: UTC ISO time the current sealed session came up; None while there is none.
        self.connected_since: str | None = None
        self.sessions_established = 0
        self.frames_rejected = 0
        #: B2BNET-16: room frames and bytes this side put on and took off the relay (all sessions);
        #: what the relay's per-room budget is sized against.
        self.traffic = {"frames_sent": 0, "bytes_sent": 0, "frames_received": 0, "bytes_received": 0}
        #: Test-only: every frame this side put on the wire (the replay test re-sends them).
        self.wire_log: list[bytes] | None = [] if record_wire else None
        self._thread = threading.Thread(target=self._run, name="b2b-internet-door", daemon=True)

    # ------------------------------------------------------------ lifecycle

    def start(self) -> DoorChannel:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stopping.set()
        with self._lock:
            ws = self._ws
        if ws is not None:
            ws.close()
        self._thread.join(timeout=5)
        self._pool.shutdown(wait=False, cancel_futures=True)

    @property
    def connected(self) -> bool:
        return self._established.is_set()

    def wait_connected(self, timeout: float) -> bool:
        return self._established.wait(timeout)

    def status(self) -> dict[str, Any]:
        with self._lock:
            relay_connected = self._ws is not None
        return {"connected": self.connected, "peer_present": self._peer_present, "relay_connected": relay_connected,
                "connected_since": self.connected_since if self.connected else None,
                "sessions_established": self.sessions_established, "frames_rejected": self.frames_rejected,
                "last_error": self.last_error, **self.traffic}

    # ------------------------------------------------------------ outgoing requests

    def request(self, request: DoorRequest, timeout: float) -> DoorAnswer:
        if not self.wait_connected(timeout):
            raise PairingError("peer_unreachable", f"no internet session with the partner within {timeout:g}s "
                               f"({self.last_error or 'partner not connected to the relay'})", 504)
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            done: tuple[threading.Event, list[DoorAnswer]] = (threading.Event(), [])
            self._waiters[request_id] = done
        try:
            self._send_data(request.to_wire(request_id))
            if not done[0].wait(timeout):
                raise PairingError("peer_unreachable", f"the partner did not answer {request.path} within {timeout:g}s", 504)
            return done[1][0]
        finally:
            with self._lock:
                self._waiters.pop(request_id, None)

    # ------------------------------------------------------------ socket loop

    def _run(self) -> None:
        attempt = 0
        while not self._stopping.is_set():
            try:
                ws = _open_socket(self.base, f"/v1/rooms/{self.pairing.room}/ws")
            except PairingError as exc:
                self.last_error = f"{exc.code}: {exc}"
                self._stopping.wait(RECONNECT_BACKOFF_S[min(attempt, len(RECONNECT_BACKOFF_S) - 1)])
                attempt += 1
                continue
            attempt = 0
            with self._lock:
                self._ws = ws
            try:
                self._pump(ws)
            except ConnectionClosed as exc:
                self.last_error = f"relay connection closed: {exc}"
            finally:
                with self._lock:
                    self._ws = None
                    self._drop_session()
                ws.close()
            if not self._stopping.is_set():
                self._stopping.wait(RECONNECT_BACKOFF_S[0])

    def _pump(self, ws: ClientConnection) -> None:
        last_hs1 = 0.0
        while not self._stopping.is_set():
            try:
                message = ws.recv(timeout=HANDSHAKE_RETRY_S / 2)
            except TimeoutError:
                message = None
            if isinstance(message, str):
                control = json.loads(message)
                if control.get("t") == "peer":
                    self._peer_present = bool(control.get("present"))
                    with self._lock:
                        self._drop_session()
                    last_hs1 = 0.0
            elif isinstance(message, bytes):
                self.traffic["frames_received"] += 1
                self.traffic["bytes_received"] += len(message)
                self._on_frame(message)
            if (self.pairing.role == "joiner" and self._peer_present and not self.connected
                    and time.monotonic() - last_hs1 >= HANDSHAKE_RETRY_S):
                self._send_handshake_1()
                last_hs1 = time.monotonic()

    def _drop_session(self) -> None:
        self._session = self._pending = self._initiator = None
        self._reset_inbound()
        self._established.clear()
        self.connected_since = None

    def _put(self, frame: bytes) -> None:
        with self._lock:
            ws = self._ws
            if self.wire_log is not None:
                self.wire_log.append(frame)
        if ws is None:
            raise PairingError("peer_unreachable", "not connected to the relay", 504)
        with self._send_lock:
            ws.send(frame)
            self.traffic["frames_sent"] += 1
            self.traffic["bytes_sent"] += len(frame)

    # ------------------------------------------------------------ handshake

    def _send_handshake_1(self) -> None:
        with self._lock:
            self._initiator = Initiator(self._psk, self._prologue)
            message = self._initiator.write_message_1()
        self._put(bytes([FRAME_HANDSHAKE_1]) + message)

    def _on_frame(self, frame: bytes) -> None:
        try:
            self._handle_frame(frame[0], frame[1:])
        except NoiseError as exc:
            self.frames_rejected += 1
            self.last_error = f"rejected frame: {exc}"

    def _handle_frame(self, kind: int, body: bytes) -> None:
        if kind == FRAME_HANDSHAKE_1 and self.pairing.role == "inviter":
            responder = Responder(self._psk, self._prologue)
            responder.read_message_1(body)
            reply, session = responder.write_message_2()
            with self._lock:
                self._pending = session
            self._put(bytes([FRAME_HANDSHAKE_2]) + reply)
        elif kind == FRAME_HANDSHAKE_2 and self.pairing.role == "joiner":
            with self._lock:
                initiator, self._initiator = self._initiator, None
            if initiator is None:
                raise NoiseError("handshake message 2 with no handshake in flight")
            session, _ = initiator.read_message_2(body)
            self._promote(session)
            self._send_data({"k": "hello", "device_id": self._hello.device_id, "label": self._hello.label})
        elif kind == FRAME_DATA:
            self._on_data(body)
        else:
            raise NoiseError(f"frame kind {kind} is not valid for the {self.pairing.role}")

    def _promote(self, session: Session) -> None:
        with self._lock:
            self._session, self._pending = session, None
            self._reset_inbound()
            self.sessions_established += 1
            self.connected_since = _now()
            self._established.set()

    # ------------------------------------------------------------ data frames

    def _send_data(self, message: dict[str, Any]) -> None:
        """Seal ``message`` as one or more data frames, each under the relay's frame cap."""
        payload = json.dumps(message).encode()
        if len(payload) > MAX_DOOR_MESSAGE_BYTES:
            raise PairingError("door_message_too_large", f"a {len(payload)}-byte door message is over the internet "
                               f"cap of {MAX_DOOR_MESSAGE_BYTES} bytes", 413)
        pieces = [payload[i:i + DOOR_FRAGMENT_BYTES] for i in range(0, len(payload), DOOR_FRAGMENT_BYTES)]
        header = bytes([FRAME_DATA])
        with self._send_lock:
            with self._lock:
                session = self._session
            for index, piece in enumerate(pieces):
                flag = FRAGMENT_LAST if index == len(pieces) - 1 else FRAGMENT_MORE
                with self._lock:
                    if session is None or self._session is not session:
                        raise PairingError("peer_unreachable", "no internet session (or it changed mid-message)", 504)
                    counter, sealed = session.seal(bytes([flag]) + piece, header)
                self._put(header + _COUNTER.pack(counter) + sealed)

    def _on_data(self, body: bytes) -> None:
        if len(body) < _COUNTER.size:
            raise NoiseError("data frame too short")
        counter, ciphertext = _COUNTER.unpack(body[: _COUNTER.size])[0], body[_COUNTER.size:]
        header = bytes([FRAME_DATA])
        with self._lock:
            candidates = [s for s in (self._session, self._pending) if s is not None]
        plaintext = None
        for session in candidates:
            try:
                plaintext = session.open(counter, ciphertext, header)
            except NoiseError:
                continue
            if session is not self._session:
                self._promote(session)
            break
        if plaintext is None:
            raise NoiseError(f"data frame {counter} opened under no live session (replayed, stale or forged)")
        message = self._reassemble(counter, plaintext)
        if message is not None:
            self._dispatch(json.loads(message))

    def _reset_inbound(self) -> None:
        self._inbound = bytearray()
        self._inbound_next = None
        self._inbound_resync = False

    def _reassemble(self, counter: int, plaintext: bytes) -> bytes | None:
        """Collect consecutive fragments; the whole message once its last fragment arrives, else None.

        A session's data frames carry consecutive counters (one sender, one ordered socket), so a
        gap means the relay dropped or reordered a frame: the partial message is discarded and
        frames are dropped up to the next message boundary rather than spliced together."""
        if not plaintext or plaintext[0] not in (FRAGMENT_MORE, FRAGMENT_LAST):
            with self._lock:
                self._reset_inbound()
            raise NoiseError("data frame has no fragment flag")
        with self._lock:
            if self._inbound_next is not None and counter != self._inbound_next:
                self._inbound, self._inbound_resync = bytearray(), True
            self._inbound_next = counter + 1
            if self._inbound_resync:
                self._inbound_resync = plaintext[0] == FRAGMENT_MORE
                raise NoiseError(f"data frame {counter} follows a counter gap; dropped up to the next message")
            self._inbound += plaintext[1:]
            if len(self._inbound) > MAX_DOOR_MESSAGE_BYTES:
                self._inbound, self._inbound_resync = bytearray(), plaintext[0] == FRAGMENT_MORE
                raise NoiseError(f"inbound door message is over the {MAX_DOOR_MESSAGE_BYTES}-byte cap")
            if plaintext[0] == FRAGMENT_MORE:
                return None
            message, self._inbound = bytes(self._inbound), bytearray()
        return message

    def _dispatch(self, message: dict[str, Any]) -> None:
        kind = message.get("k")
        if kind == "hello":
            self.peer = Hello(device_id=str(message["device_id"]), label=str(message["label"]))
            self._on_hello(self.peer)
            if self.pairing.role == "inviter":  # the joiner's hello opens each session; answer it once
                self._send_data({"k": "hello", "device_id": self._hello.device_id, "label": self._hello.label})
        elif kind == "req":
            self._pool.submit(self._answer_request, message)
        elif kind == "res":
            with self._lock:
                waiter = self._waiters.get(int(message["id"]))
            if waiter is not None:
                waiter[1].append(DoorAnswer.from_wire(message))
                waiter[0].set()
        else:
            raise NoiseError(f"unknown door message kind {kind!r}")

    def _answer_request(self, message: dict[str, Any]) -> None:
        request_id = int(message["id"])
        try:
            answer = self._serve(DoorRequest.from_wire(message))
        except (ValueError, KeyError, TypeError) as exc:
            answer = DoorAnswer(400, "application/json", json.dumps({"detail": f"bad door message: {exc}"}).encode())
        try:
            self._send_data(answer.to_wire(request_id))
        except PairingError as exc:
            if exc.code != "door_message_too_large":
                return  # no session to answer on; the requester times out with its own 504
            refusal = DoorAnswer(413, "application/json", json.dumps({"detail": str(exc), "code": exc.code}).encode())
            with contextlib.suppress(PairingError, ConnectionClosed):  # session gone: the requester's own 504
                self._send_data(refusal.to_wire(request_id))
        except ConnectionClosed:
            return


def with_peer(pairing: InternetPairing, hello: Hello) -> InternetPairing:
    return replace(pairing, peer_device_id=hello.device_id, peer_label=hello.label)
