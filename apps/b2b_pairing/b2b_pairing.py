"""B2BPAIR-01..03: B2B pairing core. One short code, one input, a long-lived peer token.

ADR-NEW ((private path)). The inviter shows a
6-character Crockford base32 code (``K7Q-4MX``); the joiner types it once. The code is the
password of a SPAKE2 exchange (the PAKE magic-wormhole uses; HomeKit's SRP and Apple TV's PIN
pairing are the same idea), so:

- a passive listener on the LAN learns nothing it can brute-force offline;
- an active guesser gets ONE guess per handshake, and an invite allows ``MAX_ATTEMPTS``
  handshakes, then it is closed. That cap bounds guessing, so an invite has no timer
  (the maintainer, Sun 11 Oct 2026): it stays open until it is used, cancelled or burned;
- the long-lived token travels sealed (AES-GCM) under a key derived from the SPAKE2 secret.

This module is pure: no sockets, no engine. The LAN door (``b2b_door``) carries the bytes and
the engine route (``routes/b2b_pairing.py``) owns persistence and the peer config.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from time import monotonic
from typing import Any, Literal

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from spake2 import SPAKE2_A, SPAKE2_B
from spake2.spake2 import SPAKEError

#: Crockford base32: no I, L, O or U, so a code read aloud or off a screen is unambiguous.
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LEN = 6
MAX_ATTEMPTS = 5
PAIRING_FILE = "b2b-pairing.json"
PROTOCOL_VERSION = "1"
_ID_JOINER = b"opendj-b2b/joiner"
_ID_INVITER = b"opendj-b2b/inviter"
_LOOKALIKES = str.maketrans({"I": "1", "L": "1", "O": "0"})
_PATH_RANK = {"bridge": 0, "wired": 1, "wifi": 2}

Role = Literal["inviter", "joiner"]


class PairingError(Exception):
    """A refusal with a machine-readable ``code`` and the HTTP status the route answers."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


# ---------------------------------------------------------------- the code


def generate_code() -> str:
    raw = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))
    return f"{raw[:3]}-{raw[3:]}"


def normalize_code(typed: str) -> str:
    """What the person typed -> the 6 canonical characters (case, separators, I/L/O forgiven)."""
    cleaned = "".join(ch for ch in typed.upper() if ch not in " -_.").translate(_LOOKALIKES)
    if len(cleaned) != CODE_LEN or any(ch not in ALPHABET for ch in cleaned):
        raise PairingError("bad_code_format", f"An invite code is {CODE_LEN} letters or digits, like K7Q-4MX", 422)
    return cleaned


# ---------------------------------------------------------------- crypto helpers


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode()


def _unb64(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(text.encode())
    except (ValueError, TypeError) as exc:
        raise PairingError("bad_message", f"not base64: {exc}") from exc


def _confirm(key: bytes, who: bytes) -> str:
    return _b64(hmac.new(key, b"opendj-b2b confirm " + who, hashlib.sha256).digest())


def _seal_key(key: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"opendj-b2b seal v1").derive(key)


# ---------------------------------------------------------------- persisted pairing


@dataclass(frozen=True)
class PairingRecord:
    peer_device_id: str
    peer_label: str
    peer_host: str
    peer_door_port: int
    token: str
    door_port: int
    paired_at: str
    role: Role
    #: Joiner only: the path kind of the Bonjour answer it chose (bridge, wired, wifi).
    selected_path: str | None = None

    @property
    def peer_url(self) -> str:
        return f"http://{self.peer_host}:{self.peer_door_port}"


def load_record(path: Path) -> PairingRecord | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return PairingRecord(**data)


def save_record(path: Path, record: PairingRecord) -> None:
    """Atomic, owner-only (0600): the file holds the shared token."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(asdict(record), indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def forget_record(path: Path) -> None:
    path.unlink(missing_ok=True)


# ---------------------------------------------------------------- inviter half


@dataclass(frozen=True)
class InviterPaired:
    """What the inviter learned at a successful finish; the route turns it into a record."""

    peer_device_id: str
    peer_label: str
    peer_host: str
    peer_door_port: int
    token: str


@dataclass
class _Session:
    key: bytes
    joiner: dict[str, Any]
    source_ip: str


class Invite:
    """One open invite: a code, ``MAX_ATTEMPTS`` handshakes, one session at a time; no timer (B2BPAIR-08)."""

    def __init__(self, code: str, device_id: str, label: str, clock: Callable[[], float]) -> None:
        self.code = code
        self.device_id = device_id
        self.label = label
        self._clock = clock
        self.created = clock()
        self.attempts_used = 0
        self.paired: InviterPaired | None = None
        self.cancelled = False
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.Lock()

    @classmethod
    def create(cls, device_id: str, label: str, clock: Callable[[], float] = monotonic) -> Invite:
        return cls(generate_code(), device_id, label, clock)

    @property
    def attempts_left(self) -> int:
        return max(0, MAX_ATTEMPTS - self.attempts_used)

    def is_open(self) -> bool:
        return self.paired is None and not self.cancelled and self.attempts_left > 0

    def _refuse_unless_open(self) -> None:
        if not self.is_open():
            raise PairingError("invite_closed", "This invite is closed (used, cancelled or too many wrong codes)", 410)

    def start(self, msg_a_b64: str, joiner: dict[str, Any], source_ip: str) -> dict[str, str]:
        """Joiner's SPAKE2 message in; ours and our key confirmation out. Spends one attempt."""
        with self._lock:
            self._refuse_unless_open()
            self.attempts_used += 1
            spake = SPAKE2_B(normalize_code(self.code).encode(), idA=_ID_JOINER, idB=_ID_INVITER)
            msg_b = spake.start()
            try:
                key = spake.finish(_unb64(msg_a_b64))
            except SPAKEError as exc:
                raise PairingError("bad_message", f"SPAKE2 message rejected: {exc}") from exc
            session = secrets.token_urlsafe(12)
            self._sessions = {session: _Session(key, dict(joiner), source_ip)}
            return {"session": session, "msg_b": _b64(msg_b), "confirm_b": _confirm(key, b"inviter")}

    def finish(self, session_id: str, confirm_a: str, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        """Joiner proves the same key; we answer with the token sealed under it.

        extra rides inside the same seal (B2BNET-02: the internet room id); the LAN door
        passes none, so its sealed payload is unchanged.
        """
        with self._lock:
            self._refuse_unless_open()
            session = self._sessions.pop(session_id, None)
            if session is None:
                raise PairingError("no_session", "No pairing handshake in progress with that id", 409)
            if not hmac.compare_digest(confirm_a.encode(), _confirm(session.key, b"joiner").encode()):
                raise PairingError("wrong_code", f"Wrong invite code ({self.attempts_left} tries left)", 403)
            token = secrets.token_urlsafe(32)
            self.paired = InviterPaired(
                peer_device_id=str(session.joiner.get("device_id", "")),
                peer_label=str(session.joiner.get("label", "")),
                peer_host=session.source_ip,
                peer_door_port=int(session.joiner["door_port"]),
                token=token,
            )
            nonce = secrets.token_bytes(12)
            payload = json.dumps({**(extra or {}), "token": token, "device_id": self.device_id, "label": self.label}).encode()
            sealed = AESGCM(_seal_key(session.key)).encrypt(nonce, payload, b"opendj-b2b")
            return {"nonce": _b64(nonce), "sealed": _b64(sealed)}


# ---------------------------------------------------------------- joiner half


@dataclass(frozen=True)
class JoinOutcome:
    token: str
    inviter_device_id: str
    inviter_label: str
    #: Whatever else the inviter sealed beside the token (B2BNET-02: the internet room id).
    extra: dict[str, str] = field(default_factory=dict)


class JoinerHandshake:
    def __init__(self, typed_code: str) -> None:
        self._spake = SPAKE2_A(normalize_code(typed_code).encode(), idA=_ID_JOINER, idB=_ID_INVITER)
        self._msg_a = self._spake.start()
        self._key: bytes | None = None

    def message(self) -> str:
        return _b64(self._msg_a)

    def check_inviter(self, msg_b_b64: str, confirm_b: str) -> str:
        """Derive the key; a confirmation mismatch means the code was wrong (for THIS inviter)."""
        try:
            key = self._spake.finish(_unb64(msg_b_b64))
        except SPAKEError as exc:
            raise PairingError("bad_message", f"SPAKE2 message rejected: {exc}") from exc
        if not hmac.compare_digest(confirm_b.encode(), _confirm(key, b"inviter").encode()):
            raise PairingError("wrong_code", "Wrong invite code", 403)
        self._key = key
        return _confirm(key, b"joiner")

    def open_sealed(self, sealed: dict[str, str]) -> JoinOutcome:
        if self._key is None:
            raise PairingError("bad_message", "open_sealed before check_inviter")
        try:
            plain = AESGCM(_seal_key(self._key)).decrypt(_unb64(sealed["nonce"]), _unb64(sealed["sealed"]), b"opendj-b2b")
        except (InvalidTag, KeyError, ValueError) as exc:
            raise PairingError("bad_seal", "The sealed token did not open: the exchange was tampered with", 502) from exc
        data = json.loads(plain)
        extra = {k: str(v) for k, v in data.items() if k not in ("token", "device_id", "label")}
        return JoinOutcome(token=data["token"], inviter_device_id=data["device_id"], inviter_label=data["label"], extra=extra)


# ---------------------------------------------------------------- path preference


def rank_addresses(candidates: list[tuple[str, str | None]]) -> list[tuple[str, str]]:
    """``(ip, path kind)`` -> LAN paths only, cable bridge first, then wired, then Wi-Fi.

    VPN tunnels and unclassified interfaces (loopback, AWDL) are dropped: Link itself is
    LAN multicast, so a pairing that only works over a VPN would pair two laptops Link
    cannot join.
    """
    usable = [(ip, kind) for ip, kind in candidates if kind in _PATH_RANK]
    return sorted(usable, key=lambda pair: _PATH_RANK[pair[1]])  # type: ignore[index]
