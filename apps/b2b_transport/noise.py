"""B2BNET-03: Noise ``NNpsk0`` per-session keys for the internet door (ADR-0145-b2b-internet-collab).

``Noise_NNpsk0_25519_ChaChaPoly_SHA256`` from the Noise Protocol Framework (rev 34), checked
against the cacophony test vector in ``tests/b2b_transport/test_noise.py``. Every connect and
reconnect runs a fresh handshake: new X25519 ephemerals, the pairing token (stretched by HKDF)
as the pre-shared key, so each session gets its own pair of direction keys and forward secrecy.

Transport frames carry an EXPLICIT 64-bit counter (the nonce) and the receiver keeps a sliding
``ReplayWindow``, as WireGuard does, because the later direct UDP path reorders and drops.
The relay carries ciphertext only; it cannot replay a frame (window), splice an earlier
session (different keys) or read one (no key).

Built on ``cryptography`` primitives only (X25519, ChaCha20-Poly1305, HMAC-SHA256).
"""

from __future__ import annotations

import hashlib
import hmac
import struct
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

PROTOCOL_NAME = b"Noise_NNpsk0_25519_ChaChaPoly_SHA256"
DHLEN = 32
HASHLEN = 32
TAGLEN = 16
#: Distinct from ADR-0129's ``opendj-b2b relay v1`` so the two channels never share a key.
PSK_LABEL = b"opendj-b2b internet psk v1"
PROLOGUE_LABEL = b"opendj-b2b internet v1\x00"
#: Nonce 2**64-1 is reserved by the Noise spec; a session never gets near it.
MAX_COUNTER = 2**64 - 2
REPLAY_WINDOW_BITS = 2048


class NoiseError(Exception):
    """A handshake or frame that does not authenticate. The frame is dropped, never parsed."""


# ---------------------------------------------------------------- primitives


def _hmac(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def _hkdf(chaining_key: bytes, ikm: bytes, outputs: int) -> list[bytes]:
    """The Noise HKDF (section 4.3): HMAC-SHA256 extract, then 2 or 3 chained expands."""
    temp = _hmac(chaining_key, ikm)
    out: list[bytes] = []
    prev = b""
    for index in range(1, outputs + 1):
        prev = _hmac(temp, prev + bytes([index]))
        out.append(prev)
    return out


def _nonce(counter: int) -> bytes:
    return b"\x00\x00\x00\x00" + struct.pack("<Q", counter)


def _public_bytes(key: X25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def derive_psk(token: str) -> bytes:
    """The pairing token (long-lived, never on the wire) -> the 32-byte Noise PSK."""
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=PSK_LABEL).derive(token.encode())


def prologue_for(room: str) -> bytes:
    """Binds the handshake to this protocol and this room: a frame from another room fails."""
    return PROLOGUE_LABEL + room.encode()


# ---------------------------------------------------------------- symmetric state


@dataclass
class _Symmetric:
    h: bytes
    ck: bytes
    k: bytes | None = None
    n: int = 0

    @classmethod
    def start(cls, prologue: bytes) -> _Symmetric:
        h = PROTOCOL_NAME.ljust(HASHLEN, b"\x00") if len(PROTOCOL_NAME) <= HASHLEN else hashlib.sha256(PROTOCOL_NAME).digest()
        state = cls(h=h, ck=h)
        state.mix_hash(prologue)
        return state

    def mix_hash(self, data: bytes) -> None:
        self.h = hashlib.sha256(self.h + data).digest()

    def mix_key(self, ikm: bytes) -> None:
        self.ck, temp_k = _hkdf(self.ck, ikm, 2)
        self.k, self.n = temp_k, 0

    def mix_key_and_hash(self, ikm: bytes) -> None:
        self.ck, temp_h, temp_k = _hkdf(self.ck, ikm, 3)
        self.mix_hash(temp_h)
        self.k, self.n = temp_k, 0

    def encrypt_and_hash(self, plaintext: bytes) -> bytes:
        assert self.k is not None, "NNpsk0 always has a key before its first payload"
        ciphertext = ChaCha20Poly1305(self.k).encrypt(_nonce(self.n), plaintext, self.h)
        self.n += 1
        self.mix_hash(ciphertext)
        return ciphertext

    def decrypt_and_hash(self, ciphertext: bytes) -> bytes:
        assert self.k is not None, "NNpsk0 always has a key before its first payload"
        try:
            plaintext = ChaCha20Poly1305(self.k).decrypt(_nonce(self.n), ciphertext, self.h)
        except InvalidTag as exc:
            raise NoiseError("handshake payload did not authenticate (wrong pairing, or tampered)") from exc
        self.n += 1
        self.mix_hash(ciphertext)
        return plaintext

    def split(self) -> tuple[bytes, bytes]:
        first, second = _hkdf(self.ck, b"", 2)
        return first, second


# ---------------------------------------------------------------- replay window


@dataclass
class ReplayWindow:
    """RFC 6479-style sliding window over explicit counters: each counter is accepted once."""

    size: int = REPLAY_WINDOW_BITS
    top: int = -1
    bits: int = 0

    def would_accept(self, counter: int) -> bool:
        if counter > self.top:
            return True
        if self.top - counter >= self.size:
            return False
        return not (self.bits >> (self.top - counter)) & 1

    def mark(self, counter: int) -> None:
        """Call only after the frame authenticated, so a forged counter cannot move the window."""
        if counter > self.top:
            shift = counter - self.top
            self.bits = ((self.bits << shift) | 1) & ((1 << self.size) - 1) if shift < self.size else 1
            self.top = counter
        else:
            self.bits |= 1 << (self.top - counter)


# ---------------------------------------------------------------- transport


@dataclass
class Session:
    """One authenticated session: a send key, a receive key, an outgoing counter, a replay window."""

    send_key: bytes
    recv_key: bytes
    handshake_hash: bytes
    send_counter: int = 0
    window: ReplayWindow = field(default_factory=ReplayWindow)

    def seal(self, plaintext: bytes, header: bytes) -> tuple[int, bytes]:
        """-> (counter, ciphertext). ``header`` is authenticated, not encrypted."""
        if self.send_counter > MAX_COUNTER:
            raise NoiseError("session counter exhausted; reconnect for fresh keys")
        counter = self.send_counter
        self.send_counter += 1
        return counter, ChaCha20Poly1305(self.send_key).encrypt(_nonce(counter), plaintext, header + struct.pack(">Q", counter))

    def open(self, counter: int, ciphertext: bytes, header: bytes) -> bytes:
        if not 0 <= counter <= MAX_COUNTER or not self.window.would_accept(counter):
            raise NoiseError(f"replayed or stale frame (counter {counter})")
        try:
            plaintext = ChaCha20Poly1305(self.recv_key).decrypt(_nonce(counter), ciphertext, header + struct.pack(">Q", counter))
        except InvalidTag as exc:
            raise NoiseError("frame did not authenticate under this session's key") from exc
        self.window.mark(counter)
        return plaintext


# ---------------------------------------------------------------- handshake (-> psk, e  <- e, ee)


class Initiator:
    """The side that speaks first. In our channel that is always the joiner."""

    def __init__(self, psk: bytes, prologue: bytes, ephemeral: X25519PrivateKey | None = None) -> None:
        self._sym = _Symmetric.start(prologue)
        self._psk = psk
        self._e = ephemeral or X25519PrivateKey.generate()

    def write_message_1(self, payload: bytes = b"") -> bytes:
        self._sym.mix_key_and_hash(self._psk)
        e_pub = _public_bytes(self._e)
        self._sym.mix_hash(e_pub)
        self._sym.mix_key(e_pub)
        return e_pub + self._sym.encrypt_and_hash(payload)

    def read_message_2(self, message: bytes) -> tuple[Session, bytes]:
        if len(message) < DHLEN + TAGLEN:
            raise NoiseError("handshake message 2 too short")
        re_pub = message[:DHLEN]
        self._sym.mix_hash(re_pub)
        self._sym.mix_key(re_pub)
        self._sym.mix_key(self._e.exchange(X25519PublicKey.from_public_bytes(re_pub)))
        payload = self._sym.decrypt_and_hash(message[DHLEN:])
        to_responder, to_initiator = self._sym.split()
        return Session(send_key=to_responder, recv_key=to_initiator, handshake_hash=self._sym.h), payload


class Responder:
    """The side that answers. In our channel that is always the inviter."""

    def __init__(self, psk: bytes, prologue: bytes, ephemeral: X25519PrivateKey | None = None) -> None:
        self._sym = _Symmetric.start(prologue)
        self._psk = psk
        self._e = ephemeral or X25519PrivateKey.generate()
        self._re: bytes | None = None

    def read_message_1(self, message: bytes) -> bytes:
        if len(message) < DHLEN + TAGLEN:
            raise NoiseError("handshake message 1 too short")
        self._sym.mix_key_and_hash(self._psk)
        self._re = message[:DHLEN]
        self._sym.mix_hash(self._re)
        self._sym.mix_key(self._re)
        return self._sym.decrypt_and_hash(message[DHLEN:])

    def write_message_2(self, payload: bytes = b"") -> tuple[bytes, Session]:
        assert self._re is not None, "read_message_1 first"
        e_pub = _public_bytes(self._e)
        self._sym.mix_hash(e_pub)
        self._sym.mix_key(e_pub)
        self._sym.mix_key(self._e.exchange(X25519PublicKey.from_public_bytes(self._re)))
        message = e_pub + self._sym.encrypt_and_hash(payload)
        to_responder, to_initiator = self._sym.split()
        return message, Session(send_key=to_initiator, recv_key=to_responder, handshake_hash=self._sym.h)
