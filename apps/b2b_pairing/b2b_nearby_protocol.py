"""B2B nearby wire constants, crypto helpers, and record types."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from urllib.parse import quote

from apps.b2b_pairing import b2b_mdns
from apps.b2b_pairing.b2b_pairing import PROTOCOL_VERSION, PairingError

NEARBY_VERSION = "1"
SETTINGS_FILE = "b2b-nearby.json"
REQUEST_TTL_S = 60.0
RATE_WINDOW_S = 60.0
RATE_PER_SOURCE = 3
MAX_PENDING = 4
CHALLENGE_TTL_S = 30.0
MAX_CHALLENGES = 16
SCAN_MAX_AGE_S = 4.0
#: B2BNEAR-06: a scan still running this long after it started is reported timed out and
#: abandoned (its late result is dropped). Covers the mDNS budget plus the hardware-port read.
SCAN_DEADLINE_S = b2b_mdns.SCAN_BUDGET_S + 5.0
DOOR_TIMEOUT_S = 3.0
LABEL_MAX = 64

REQUEST = "/b2b-nearby/request"
CANCEL = "/b2b-nearby/cancel"
ANSWER = "/b2b-nearby/answer"
CHALLENGE = "/b2b-nearby/challenge"
RECONNECT = "/b2b-nearby/reconnect"


OutgoingState = Literal["pending", "accepted", "declined", "expired", "cancelled", "failed"]


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode()


def _unb64(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(str(text).encode())
    except (ValueError, TypeError) as exc:
        raise PairingError("bad_message", f"not base64: {exc}") from exc


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def pairing_tag(token: str) -> str:
    """16 hex chars both partners derive from their shared token; meaningless to anyone else."""
    return hmac.new(token.encode(), b"opendj-b2b nearby tag v1", hashlib.sha256).hexdigest()[:16]


def build_txt(device_id: str, label: str, accepting: bool, tag: str | None, pub: bytes) -> dict[str, str]:
    txt = {
        "v": PROTOCOL_VERSION,
        "id": device_id,
        "nv": NEARBY_VERSION,
        "name": quote(label, safe=""),
        "acc": "1" if accepting else "0",
        "pk": _b64(pub),
    }
    if tag is not None:
        txt["pt"] = tag
    return txt


def reconnect_proof(
    token: str, side: bytes, nonce: str, challenger_id: str, dialed: str, asker_id: str, door_port: int
) -> str:
    """HMAC under the pairing token over a fresh challenge nonce: ``side`` is ``b"asker"`` or ``b"answerer"``.

    B2BREADDR-03: the proof also names the exchange it was made for: ``dialed`` is the answerer's
    door as the asker dialed it (``host:port``), ``asker_id`` and ``door_port`` are the asker's
    device id and its own door. A host that passes the exchange on to the real partner hands it a
    proof for another door, and nobody on the way can change where the asker will be looked for."""
    fields = [side.decode(), nonce, challenger_id, dialed, asker_id, door_port]
    message = b"opendj-b2b reconnect v2 " + json.dumps(fields, separators=(",", ":")).encode()
    return _b64(hmac.new(token.encode(), message, hashlib.sha256).digest())


def _seal_key(private: X25519PrivateKey, peer_public: bytes, request_id: str) -> bytes:
    try:
        shared = private.exchange(X25519PublicKey.from_public_bytes(peer_public))
    except ValueError as exc:
        raise PairingError("bad_message", f"not an X25519 public key: {exc}") from exc
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=request_id.encode(), info=b"opendj-b2b nearby seal v1"
    ).derive(shared)


def seal_token(
    private: X25519PrivateKey, peer_public: bytes, request_id: str, payload: dict[str, Any]
) -> dict[str, str]:
    nonce = secrets.token_bytes(12)
    sealed = AESGCM(_seal_key(private, peer_public, request_id)).encrypt(
        nonce, json.dumps(payload).encode(), request_id.encode()
    )
    return {"pub": _b64(private.public_key().public_bytes_raw()), "nonce": _b64(nonce), "sealed": _b64(sealed)}


def open_token(
    private: X25519PrivateKey, request_id: str, answer: dict[str, Any], peer_public: bytes
) -> dict[str, Any]:
    claimed = answer.get("pub")
    if claimed is not None:
        try:
            claimed_pub = _unb64(str(claimed))
        except PairingError as exc:
            raise PairingError("wrong_answerer", "The answer did not come from the laptop that was asked", 403) from exc
        if len(claimed_pub) != 32 or len(peer_public) != 32 or not hmac.compare_digest(claimed_pub, peer_public):
            raise PairingError("wrong_answerer", "The answer did not come from the laptop that was asked", 403)
    try:
        key = _seal_key(private, peer_public, request_id)
        plain = AESGCM(key).decrypt(_unb64(answer["nonce"]), _unb64(answer["sealed"]), request_id.encode())
    except (InvalidTag, KeyError) as exc:
        raise PairingError("bad_seal", "The sealed token did not open: the answer was tampered with", 400) from exc
    data = json.loads(plain)
    if not isinstance(data, dict) or not all(isinstance(data.get(k), str) for k in ("token", "device_id", "label")):
        raise PairingError("bad_seal", "The sealed answer is not a pairing", 400)
    return data


@dataclass(frozen=True)
class NearbyPeer:
    id: str
    label: str
    accepting: bool
    remembered: bool
    port: int
    #: (ip, path kind) best first: cable bridge, wired, Wi-Fi.
    addresses: list[tuple[str, str]]
    instance: str
    #: THIS laptop's interface the best address answered on (the path is named from this side).
    via: str | None = None
    #: Advertised X25519 identity public (TXT ``pk``); None when missing or unusable.
    pub: bytes | None = None

    def view(self) -> dict[str, Any]:
        best = self.addresses[0] if self.addresses else (None, None)
        return {
            "id": self.id,
            "label": self.label,
            "accepting": self.accepting,
            "remembered": self.remembered,
            "address": best[0],
            "path": best[1],
            "via": self.via,
            "port": self.port,
        }


@dataclass
class IncomingRequest:
    id: str
    device_id: str
    label: str
    source_ip: str
    door_port: int
    pub: bytes
    received: float
    received_at: str


@dataclass
class OutgoingRequest:
    id: str
    to_id: str
    to_label: str
    host: str
    port: int
    path: str
    private: X25519PrivateKey
    deadline: float
    peer_pub: bytes
    state: OutgoingState = "pending"
    detail: str | None = None
    created_at: str = field(default_factory=_now_iso)


def _is_ipv4(text: str) -> bool:
    try:
        return ipaddress.ip_address(text).version == 4
    except ValueError:
        return False


def _first_per_id(peers: list[NearbyPeer]) -> list[NearbyPeer]:
    seen: dict[str, NearbyPeer] = {}
    for peer in peers:
        seen.setdefault(peer.id, peer)
    return list(seen.values())
