"""B2BBOOTH-97: sealed UDP datagrams between two paired engines on a LAN (the booth's datagram lane).

Contract v3 "Engine-to-engine channel": the documented fallback when control latency under 1% loss
fails its bar over the reliable door (measured Thu 15 Oct 2026: joiner -> inviter p95 103 and 127 ms
against about 102 ms at 25 ms / 1% loss; one lost TCP segment waits for a retransmit). Continuous
controls, position samples, renews and clock pings ride this; discrete ops stay on the door.

No new crypto: the frame is sealed by ``noise.Session`` (ChaCha20-Poly1305 with an explicit 64-bit
counter as the nonce, the RFC 6479 ``ReplayWindow`` on receive) under keys derived with HKDF-SHA256
from the pairing token, exactly as ``noise.derive_psk`` stretches it, with a booth-specific label:

    key(direction) = HKDF-SHA256(ikm = token, salt = epoch || sender_sid,
                                 info = "opendj-b2b booth datagram v1 " || direction)

- ``epoch`` (the booth epoch, 8 bytes) and ``sender_sid`` (8 random bytes minted per epoch by the
  sender, announced to the partner over the authenticated door) make every sender session's key
  fresh, so a restarted sender never reuses a (key, counter) pair and an old session's frames do
  not authenticate under the new one.
- ``direction`` is the SENDER's side (inviter | joiner): a frame reflected back to its sender does
  not authenticate there.

Frame: ``MAGIC(4) | epoch(8) | sender_sid(8) | counter(8, big-endian) | ciphertext+tag``. The first
28 bytes are the authenticated header. A frame that does not open is dropped and counted by reason
(``malformed``, ``wrong_epoch``, ``unknown_sender``, ``replayed``, ``tampered``), never parsed.

``SealedDatagrams`` is pure (no socket); ``DatagramSocket`` is its one receive thread, inside the
engine process (no new process).
"""

from __future__ import annotations

import errno
import secrets
import select
import socket
import struct
import threading
from collections.abc import Callable
from typing import Any, Literal, cast

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from apps.b2b_transport.noise import NoiseError, ReplayWindow, Session

Direction = Literal["inviter", "joiner"]
Address = tuple[str, int]
#: Called with (counter, plaintext, source address) for every frame that authenticated.
OnPayload = Callable[[int, bytes, Address], None]

MAGIC = b"OBD1"
LABEL = b"opendj-b2b booth datagram v1 "
HEADER = struct.Struct(">4s8s8sQ")
TAG_BYTES = 16
#: Stays under one Ethernet MTU (1500) less IPv6 + UDP headers, so a datagram is never fragmented.
MAX_DATAGRAM = 1200
MAX_PAYLOAD = MAX_DATAGRAM - HEADER.size - TAG_BYTES
RECV_WAKE_S = 0.25
DROP_REASONS = ("malformed", "wrong_epoch", "unknown_sender", "replayed", "tampered", "no_session")
_DIRECTIONS = frozenset({"inviter", "joiner"})


class DatagramError(ValueError):
    pass


def _require_direction(value: str, *, field: str) -> Direction:
    if value not in _DIRECTIONS:
        raise DatagramError(f"{field} must be inviter or joiner, got {value!r}")
    return cast(Direction, value)


def derive_key(token: str, epoch: str, sender_sid: str, direction: Direction) -> bytes:
    """The 32-byte ChaCha20-Poly1305 key of one sender session (hex ``epoch`` and ``sender_sid``)."""
    _require_direction(direction, field="direction")
    salt = bytes.fromhex(epoch) + bytes.fromhex(sender_sid)
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=LABEL + direction.encode()).derive(token.encode())


def _other(side: Direction) -> Direction:
    return "joiner" if side == "inviter" else "inviter"


class SealedDatagrams:
    """One laptop's sealing state for the current booth epoch: its own send session and the
    partner's announced receive session. Not thread-safe; ``DatagramSocket`` serializes it."""

    def __init__(self, token: str, side: Direction) -> None:
        if not token:
            raise DatagramError("a sealed datagram lane needs the pairing token")
        self.side = _require_direction(side, field="side")
        self._token = token
        self.epoch: str | None = None
        self.sid: str | None = None
        self.peer_sid: str | None = None
        self._session: Session | None = None
        self._peer_ready = False
        self.sent = 0
        self.received = 0
        self.dropped: dict[str, int] = dict.fromkeys(DROP_REASONS, 0)

    def begin(self, epoch: str, side: Direction | None = None) -> str:
        """A new epoch (or a new side): mint this sender's sid; the partner's session must be announced again."""
        if side is not None:
            self.side = side
        self.epoch, self.sid, self.peer_sid = epoch, secrets.token_hex(8), None
        send_key = derive_key(self._token, epoch, self.sid, self.side)
        self._session = Session(send_key=send_key, recv_key=bytes(32), handshake_hash=b"")
        self._peer_ready = False
        return self.sid

    def set_peer(self, epoch: str, peer_sid: str) -> bool:
        """The partner announced its sender session (over the authenticated door). True when it is new."""
        if self._session is None or epoch != self.epoch:
            return False
        if peer_sid == self.peer_sid:
            return False
        bytes.fromhex(peer_sid)  # a malformed sid raises before it is used
        self.peer_sid = peer_sid
        self._session.recv_key = derive_key(self._token, epoch, peer_sid, _other(self.side))
        self._session.window = ReplayWindow()
        self._peer_ready = True
        return True

    @property
    def ready(self) -> bool:
        return self._session is not None and self._peer_ready

    def seal(self, payload: bytes) -> bytes:
        if self._session is None or self.epoch is None or self.sid is None:
            raise DatagramError("no epoch yet: begin() first")
        if len(payload) > MAX_PAYLOAD:
            raise DatagramError(f"payload {len(payload)} bytes is over the {MAX_PAYLOAD}-byte datagram budget")
        counter = self._session.send_counter
        header = HEADER.pack(MAGIC, bytes.fromhex(self.epoch), bytes.fromhex(self.sid), counter)
        sealed_counter, ciphertext = self._session.seal(payload, header[:-8])
        assert sealed_counter == counter
        self.sent += 1
        return header + ciphertext

    def open(self, frame: bytes) -> tuple[int, bytes] | None:
        """-> (counter, plaintext), or None with the drop counted by its reason."""
        if len(frame) < HEADER.size + TAG_BYTES:
            return self._drop("malformed")
        magic, epoch, sid, counter = HEADER.unpack_from(frame)
        if magic != MAGIC:
            return self._drop("malformed")
        if self._session is None or self.epoch is None:
            return self._drop("no_session")
        if epoch != bytes.fromhex(self.epoch):
            return self._drop("wrong_epoch")
        if not self._peer_ready or self.peer_sid is None or sid != bytes.fromhex(self.peer_sid):
            return self._drop("unknown_sender")
        if not self._session.window.would_accept(counter):
            return self._drop("replayed")
        try:
            plaintext = self._session.open(counter, frame[HEADER.size:], frame[: HEADER.size - 8])
        except NoiseError:
            return self._drop("tampered")
        self.received += 1
        return counter, plaintext

    def _drop(self, reason: str) -> None:
        self.dropped[reason] += 1

    def status(self) -> dict[str, Any]:
        return {"epoch": self.epoch, "sid": self.sid, "peer_sid": self.peer_sid, "sent": self.sent,
                "received": self.received, "dropped": dict(self.dropped)}


class DatagramSocket:
    """One UDP socket and its receive thread (``b2b-booth-dgram``), inside the engine process.

    ``peer_host`` is the partner's address from the pairing (the LAN door URL's host); its UDP port
    arrives over the door (``set_peer``). Sends never block: a full socket buffer drops the datagram
    and counts it (the lane is newest-wins, never retransmitted)."""

    def __init__(self, token: str, side: Direction, peer_host: str, bind_host: str = "0.0.0.0") -> None:
        self.sealed = SealedDatagrams(token, side)
        self.peer_host = peer_host
        self.peer_port: int | None = None
        self.send_errors = 0
        self.last_error: str | None = None
        family = socket.AF_INET6 if ":" in bind_host else socket.AF_INET
        self._sock = socket.socket(family, socket.SOCK_DGRAM)
        self._sock.bind((bind_host, 0))
        # Non-blocking: a send happens under the booth's lock (and on the web server's event loop for a
        # page intent), so a full socket buffer must drop the datagram, never wait. The receive thread
        # waits in select() instead, waking every RECV_WAKE_S to notice close().
        self._sock.setblocking(False)
        self.port: int = self._sock.getsockname()[1]
        self._lock = threading.Lock()
        self._on_payload: OnPayload | None = None
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="b2b-booth-dgram", daemon=True)

    def start(self, on_payload: OnPayload) -> None:
        self._on_payload = on_payload
        self._thread.start()

    # -- sealing state (the booth drives it)

    def begin(self, epoch: str, side: Direction) -> str:
        with self._lock:
            self.peer_port = None
            return self.sealed.begin(epoch, side)

    def set_peer(self, epoch: str, port: int, sid: str) -> bool:
        with self._lock:
            if not 0 < port < 65536:
                raise DatagramError(f"peer port must be 1..65535, got {port}")
            new = self.sealed.set_peer(epoch, sid)
            if self.sealed.peer_sid == sid and self.sealed.epoch == epoch:
                self.peer_port = port
            return new

    @property
    def ready(self) -> bool:
        return self.sealed.ready and self.peer_port is not None

    # -- I/O

    def send(self, payload: bytes, addr: Address | None = None) -> bool:
        """Seal and send to ``addr`` (default: the partner). False when not ready or the send failed."""
        with self._lock:
            if self._closed or not self.sealed.ready:
                return False
            target = addr if addr is not None else (None if self.peer_port is None else (self.peer_host, self.peer_port))
            if target is None:
                return False
            frame = self.sealed.seal(payload)
        try:
            self._sock.sendto(frame, target)
        except BlockingIOError:
            self.send_errors += 1
            self.last_error = f"sendto {target}: socket buffer full, datagram dropped"
            return False
        except OSError as exc:
            self.send_errors += 1
            self.last_error = f"sendto {target}: {exc}"
            return False
        return True

    def _run(self) -> None:
        while True:
            if self._closed:
                return
            try:
                readable, _, _ = select.select([self._sock], [], [], RECV_WAKE_S)
                if not readable:
                    continue
                frame, addr = self._sock.recvfrom(MAX_DATAGRAM + 64)
            except BlockingIOError:
                continue
            except (OSError, ValueError) as exc:  # ValueError: select() on a socket closed meanwhile
                if self._closed or getattr(exc, "errno", None) in (errno.EBADF, errno.ENOTSOCK):
                    return
                self.last_error = f"recvfrom: {exc}"
                continue
            with self._lock:
                opened = self.sealed.open(frame)
            if opened is not None and self._on_payload is not None:
                self._on_payload(opened[0], opened[1], (addr[0], addr[1]))

    def close(self) -> None:
        with self._lock:
            self._closed = True
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=2 * RECV_WAKE_S + 1)
        self._sock.close()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"port": self.port, "peer": None if self.peer_port is None else f"{self.peer_host}:{self.peer_port}",
                    **self.sealed.status(), "send_errors": self.send_errors, "last_error": self.last_error}
