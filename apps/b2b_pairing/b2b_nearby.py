"""B2BNEAR-02..04: Open DJs nearby, Ask to B2B with Accept / Decline, and Reconnect.

ADR-NEW ((private path)). One per engine, beside the pairing
service (``b2b_service.PairingService``), whose LAN door and pairing record it reuses:

- **Listed nearby.** While running it advertises ``_opendj-b2b._tcp`` with TXT ``v``, ``id``
  (the per-boot device id), ``nv`` (this protocol), ``name`` (the DJ name, percent-encoded),
  ``acc`` (accepting requests, 1/0) and, when paired, ``pt`` (a tag derived from the shared
  token, so only the remembered partner recognizes it). It browses the same type and lists
  every OTHER instance that carries ``nv``.
- **Ask to B2B.** The asker posts ``/b2b-nearby/request`` to the other door with an X25519
  public key. The other laptop holds it as an incoming request (60 s, rate-limited) until a
  person presses Accept or Decline. Accept generates the long-lived token, seals it under an
  ECDH key (X25519, HKDF-SHA256, AES-GCM, the request id as associated data) and posts it to
  the asker's door ``/b2b-nearby/answer``; both then hold the same pairing record the invite
  code makes. Decline tells the asker only ``declined``. Nothing pairs without Accept.
- **Reconnect.** Two laptops that both still hold the same token prove it to each other by
  HMAC challenge-response (``/b2b-nearby/challenge`` then ``/b2b-nearby/reconnect``) and
  refresh each other's address. No prompt, no code; a laptop that forgot answers
  ``not_remembered``. A Reconnect press (or its CLI/MCP twin) runs it, and so does the peer
  watch (``b2b_readdress``, B2BREADDR-01) when the partner stops answering where it was. The
  proof names the door that was dialed, so a relayed one is refused (B2BREADDR-03).

Every refusal is a ``PairingError`` with a named ``code`` and the HTTP status to answer.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any, Literal
from urllib.parse import quote, unquote

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from apps.ableton_link.link_transports import classify_interface
from apps.b2b_pairing import b2b_mdns
from apps.b2b_pairing.b2b_pairing import PROTOCOL_VERSION, PairingError, PairingRecord, rank_addresses

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

log = logging.getLogger(__name__)

OutgoingState = Literal["pending", "accepted", "declined", "expired", "cancelled", "failed"]


# ---------------------------------------------------------------- pure helpers


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


def build_txt(device_id: str, label: str, accepting: bool, tag: str | None) -> dict[str, str]:
    txt = {
        "v": PROTOCOL_VERSION,
        "id": device_id,
        "nv": NEARBY_VERSION,
        "name": quote(label, safe=""),
        "acc": "1" if accepting else "0",
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


def open_token(private: X25519PrivateKey, request_id: str, answer: dict[str, Any]) -> dict[str, Any]:
    try:
        key = _seal_key(private, _unb64(answer["pub"]), request_id)
        plain = AESGCM(key).decrypt(_unb64(answer["nonce"]), _unb64(answer["sealed"]), request_id.encode())
    except (InvalidTag, KeyError) as exc:
        raise PairingError("bad_seal", "The sealed token did not open: the answer was tampered with", 400) from exc
    data = json.loads(plain)
    if not isinstance(data, dict) or not all(isinstance(data.get(k), str) for k in ("token", "device_id", "label")):
        raise PairingError("bad_seal", "The sealed answer is not a pairing", 400)
    return data


# ---------------------------------------------------------------- records


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
    state: OutgoingState = "pending"
    detail: str | None = None
    created_at: str = field(default_factory=_now_iso)


# ---------------------------------------------------------------- service


class NearbyService:
    """Advertise, browse, ask, answer and reconnect. Network calls run outside ``self._lock``."""

    def __init__(
        self,
        pairing: Any,
        state_dir: Path,
        hardware_ports: Callable[[], dict[str, str]],
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.pairing = pairing
        self.settings_path = state_dir / SETTINGS_FILE
        self._hardware_ports = hardware_ports
        self._clock = clock
        self._lock = threading.RLock()
        self.accepting = self._load_accepting()
        self._advertiser: b2b_mdns.Advertiser | None = None
        self._advertised_txt: dict[str, str] | None = None
        self._peers: list[NearbyPeer] = []
        #: B2BREADDR-02: every instance of the last scan that carries this pairing's tag. Not
        #: deduplicated by id: a host that copies the partner's tag AND id must not hide the partner.
        self._partner_candidates: list[NearbyPeer] = []
        self._scanned: float | None = None
        self._scanned_at: str | None = None
        self._scan_error: str | None = None
        self._scan_note: str | None = None
        self._scan_thread: threading.Thread | None = None
        self._scan_started: float | None = None
        #: Bumped per scan and on abandoning one, so only the current scan may write its result.
        self._scan_generation = 0
        self._incoming: dict[str, IncomingRequest] = {}
        self._recent: dict[str, deque[float]] = {}
        self._challenges: dict[str, float] = {}
        self.outgoing: OutgoingRequest | None = None

    # ------------------------------------------------------------ lifecycle

    def running(self) -> bool:
        return self._advertiser is not None

    def needs_door(self) -> bool:
        outgoing = self.outgoing
        return self.running() or (outgoing is not None and outgoing.state == "pending")

    def start(self) -> None:
        if not b2b_mdns.available():
            raise PairingError("mdns_unavailable", f"Bonjour ({b2b_mdns.client_name()}) is not available on this machine", 501)
        with self._lock:
            self.pairing.nearby = self
            self._sync_advert()

    def stop(self) -> None:
        with self._lock:
            self._withdraw()
            self.pairing.close_door_if_idle()

    def pairing_changed(self) -> None:
        with self._lock:
            if self.running():
                self._sync_advert()

    def _withdraw(self) -> None:
        if self._advertiser is not None:
            self._advertiser.stop()
        self._advertiser = None
        self._advertised_txt = None

    def _sync_advert(self) -> None:
        """(Re-)register when the TXT no longer says what is true (name, accepting, pairing tag)."""
        record = self.pairing.record
        txt = build_txt(
            self.pairing.device_id,
            self.pairing.label,
            self.accepting,
            pairing_tag(record.token) if record is not None else None,
        )
        if self._advertiser is not None and self._advertiser.alive() and txt == self._advertised_txt:
            return
        door_port = self.pairing.open_door()
        self._withdraw()
        try:
            self._advertiser = b2b_mdns.Advertiser(
                f"Open DJ {self.pairing.label} {self.pairing.device_id[:6]} n", door_port, txt
            )
        except b2b_mdns.MdnsUnavailable as exc:  # B2BPAIR-09: Avahi said why it could not register
            raise PairingError("mdns_unavailable", str(exc), 503) from exc
        self._advertised_txt = txt

    # ------------------------------------------------------------ settings

    def _load_accepting(self) -> bool:
        if not self.settings_path.exists():
            return True
        data = json.loads(self.settings_path.read_text())
        if not isinstance(data, dict) or not isinstance(data.get("accepting"), bool):
            raise TypeError(f"{self.settings_path} is not a B2B nearby settings record: {data!r}")
        return bool(data["accepting"])

    def set_accepting(self, accepting: bool) -> dict[str, Any]:
        self.start()
        with self._lock:
            self.accepting = accepting
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            self.settings_path.write_text(json.dumps({"accepting": accepting}) + "\n")
            if not accepting:
                self._incoming.clear()
            self._sync_advert()
        return self.snapshot()

    # ------------------------------------------------------------ browse

    def snapshot(self, wait: bool = False) -> dict[str, Any]:
        self.start()
        thread = self._kick_scan()
        if wait and thread is not None:
            started = self._scan_started
            thread.join(timeout=max(0.0, SCAN_DEADLINE_S - (self._clock() - started if started is not None else 0.0)))
        with self._lock:
            self._abandon_overdue_scan()
            return {
                "me": {
                    "device_id": self.pairing.device_id,
                    "label": self.pairing.label,
                    "accepting": self.accepting,
                    "listed": self.running(),
                    "door_port": self.pairing.open_door(),
                },
                "nearby": [peer.view() for peer in self._peers],
                "scanned_at": self._scanned_at,
                "scanning": self._scan_thread is not None and self._scan_thread.is_alive(),
                "scan_error": self._scan_error,
                "scan_note": self._scan_note,
                "outgoing": self._outgoing_view(),
                "incoming_count": len(self._live_incoming()),
            }

    def discovery(self, kick: bool) -> dict[str, Any]:
        """B2BMCAST-02: what the last finished scan says about the remembered partner, read
        without starting Nearby. ``kick`` starts a background scan (only while running) so the
        next call reads a fresh one; it never waits for it."""
        if kick and self.running():
            self._kick_scan()
        with self._lock:
            self._abandon_overdue_scan()
            scanned = self._scanned
            listed = any(peer.remembered for peer in self._peers)
            return {
                "running": self.running(),
                "scanned_at": scanned,
                "scan_error": self._scan_error,
                "scan_note": self._scan_note,
                # A partial scan that missed the partner cannot say it is absent (B2BNEAR-06).
                "partner_listed": None if scanned is None or self._scan_error is not None
                or (not listed and self._scan_note is not None) else listed,
            }

    def find_partner(self) -> list[NearbyPeer]:
        """B2BREADDR-02: a NEW scan's instances that carry this pairing's tag, the remembered
        partner among them if it is on this network. Blocks for the scan (inside its deadline).
        The tag is public on Bonjour, so this is where to ask, never who it is: ``prove_at`` decides."""
        self.start()
        thread = self._kick_scan(fresh=True)
        if thread is not None:
            started = self._scan_started
            thread.join(timeout=max(0.0, SCAN_DEADLINE_S - (self._clock() - started if started is not None else 0.0)))
        with self._lock:
            self._abandon_overdue_scan()
            if self._scan_error is not None:
                raise PairingError("scan_failed", self._scan_error, 503)
            return list(self._partner_candidates)

    def _kick_scan(self, fresh: bool = False) -> threading.Thread | None:
        with self._lock:
            self._abandon_overdue_scan()
            if self._scan_thread is not None and self._scan_thread.is_alive():
                return self._scan_thread
            if not fresh and self._scanned is not None and self._clock() - self._scanned < SCAN_MAX_AGE_S:
                return None
            self._sync_advert()
            self._scan_generation += 1
            generation = self._scan_generation
            self._scan_started = self._clock()
            self._scan_thread = threading.Thread(
                target=self._scan, args=(generation,), name="b2b-nearby-scan", daemon=True
            )
            self._scan_thread.start()
            return self._scan_thread

    def _abandon_overdue_scan(self) -> None:
        """Caller holds ``self._lock``. A scan past ``SCAN_DEADLINE_S`` ends as a timeout now; its
        thread is left to finish on its own and whatever it finds is dropped."""
        thread, started = self._scan_thread, self._scan_started
        if thread is None or not thread.is_alive() or started is None or self._clock() - started < SCAN_DEADLINE_S:
            return
        log.warning("b2b nearby scan still running %.1f s after it started; reporting a timeout", SCAN_DEADLINE_S)
        self._scan_generation += 1
        self._scan_thread = None
        self._scan_error = f"scan timed out after {SCAN_DEADLINE_S:g} s"
        self._scan_note = None
        self._scanned = self._clock()
        self._scanned_at = _now_iso()

    def _scan(self, generation: int) -> None:
        try:
            result = b2b_mdns.scan()
            every = self._every_peer_from(result.resolved)
        except Exception as exc:  # the thread's only exit for an error is scan_error
            log.exception("b2b nearby scan failed")
            self._finish_scan(generation, error=f"{type(exc).__name__}: {exc}")
            return
        self._finish_scan(generation, peers=every, note="; ".join(result.notes) or None)

    def _finish_scan(
        self, generation: int, *, peers: list[NearbyPeer] | None = None, note: str | None = None,
        error: str | None = None,
    ) -> None:
        with self._lock:
            if generation != self._scan_generation:
                log.warning("b2b nearby scan finished after its deadline; result dropped")
                return
            if error is None:
                every = peers or []
                self._peers = sorted(_first_per_id(every), key=lambda p: (not p.remembered, p.label.lower()))
                self._partner_candidates = [peer for peer in every if peer.remembered]
            self._scan_error = error
            self._scan_note = note
            self._scanned = self._clock()
            self._scanned_at = _now_iso()

    def _peers_from(self, found: list[b2b_mdns.Resolved]) -> list[NearbyPeer]:
        """The listing: one entry per device id, the first instance seen."""
        return _first_per_id(self._every_peer_from(found))

    def _every_peer_from(self, found: list[b2b_mdns.Resolved]) -> list[NearbyPeer]:
        """Every OTHER nearby instance, in scan order, two instances under one id included."""
        record = self.pairing.record
        own_tag = pairing_tag(record.token) if record is not None else None
        hardware = self._hardware_ports()
        peers: list[NearbyPeer] = []
        for resolved in found:
            txt = resolved.txt
            peer_id = txt.get("id", "")
            if txt.get("nv") != NEARBY_VERSION or not peer_id or peer_id == self.pairing.device_id:
                continue
            # An advert is whatever its sender typed: only IPv4 literals are addresses of a door.
            addresses = [(ip, dev) for ip, dev in resolved.addresses if _is_ipv4(ip)]
            ranked = rank_addresses([(ip, classify_interface(dev, hardware.get(dev))) for ip, dev in addresses])
            devices = dict(addresses)
            peers.append(NearbyPeer(
                id=peer_id,
                label=unquote(txt.get("name", ""))[:LABEL_MAX] or "Open DJ",
                accepting=txt.get("acc") == "1",
                remembered=own_tag is not None and hmac.compare_digest(txt.get("pt", ""), own_tag),
                port=resolved.port,
                addresses=ranked,
                instance=resolved.instance,
                via=devices[ranked[0][0]] if ranked else None,
            ))
        return peers

    def _peer(self, peer_id: str) -> NearbyPeer:
        with self._lock:
            for peer in self._peers:
                if peer.id == peer_id:
                    return peer
        raise PairingError("not_nearby", "That Open DJ is not listed nearby (any more): refresh the list", 404)

    # ------------------------------------------------------------ asker side

    def request(self, peer_id: str) -> dict[str, Any]:
        self.start()
        peer = self._peer(peer_id)
        if not peer.accepting:
            raise PairingError("not_accepting", f"{peer.label} is not accepting B2B requests", 409)
        with self._lock:
            if self.outgoing is not None and self._outgoing_state() == "pending":
                raise PairingError(
                    "request_pending", f"Already asking {self.outgoing.to_label}: cancel that first", 409
                )
        door_port = self.pairing.open_door()
        private = X25519PrivateKey.generate()
        body = {
            "v": NEARBY_VERSION,
            "device_id": self.pairing.device_id,
            "label": self.pairing.label,
            "door_port": door_port,
            "pub": _b64(private.public_key().public_bytes_raw()),
        }
        answer, host, path = _post_first(peer, REQUEST, body)
        with self._lock:
            self.outgoing = OutgoingRequest(
                id=str(answer["request_id"]),
                to_id=peer.id,
                to_label=peer.label,
                host=host,
                port=peer.port,
                path=path,
                private=private,
                deadline=self._clock() + float(answer["expires_in_s"]),
            )
        return self.snapshot()

    def cancel_request(self) -> dict[str, Any]:
        with self._lock:
            outgoing = self.outgoing
            if outgoing is None or self._outgoing_state() != "pending":
                raise PairingError("no_request", "Not asking anyone right now", 409)
            outgoing.state = "cancelled"
        try:
            _post(f"http://{outgoing.host}:{outgoing.port}{CANCEL}", {"request_id": outgoing.id})
            outgoing.detail = None
        except PairingError as exc:
            outgoing.detail = f"the other laptop was not told: {exc.code}"
        self.pairing.close_door_if_idle()
        return self.snapshot()

    def _outgoing_state(self) -> OutgoingState | None:
        outgoing = self.outgoing
        if outgoing is None:
            return None
        if outgoing.state == "pending" and self._clock() > outgoing.deadline:
            outgoing.state = "expired"
        return outgoing.state

    def _outgoing_view(self) -> dict[str, Any] | None:
        outgoing = self.outgoing
        if outgoing is None:
            return None
        state = self._outgoing_state()
        return {
            "id": outgoing.id,
            "to_id": outgoing.to_id,
            "to_label": outgoing.to_label,
            "state": state,
            "detail": outgoing.detail,
            "created_at": outgoing.created_at,
            "path": outgoing.path,
            "expires_in_s": round(max(0.0, outgoing.deadline - self._clock()), 1) if state == "pending" else 0.0,
        }

    def _receive_answer(self, body: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            outgoing = self.outgoing
            if outgoing is None or outgoing.id != str(body["request_id"]) or self._outgoing_state() != "pending":
                raise PairingError("no_request", "No pending B2B request with that id", 404)
            if body.get("accepted") is not True:
                outgoing.state = "declined"
                return {"ok": True}
            try:
                data = open_token(outgoing.private, outgoing.id, body)
            except PairingError:
                outgoing.state = "failed"
                outgoing.detail = "bad_seal"
                raise
            if data["device_id"] != outgoing.to_id:
                outgoing.state = "failed"
                outgoing.detail = "answered_by_another_laptop"
                raise PairingError("wrong_answerer", "The answer did not come from the laptop that was asked", 403)
            self.pairing.adopt(
                PairingRecord(
                    peer_device_id=data["device_id"],
                    peer_label=data["label"][:LABEL_MAX],
                    peer_host=outgoing.host,
                    peer_door_port=outgoing.port,
                    token=data["token"],
                    door_port=self.pairing.open_door(),
                    paired_at=_now_iso(),
                    role="joiner",
                    selected_path=outgoing.path,
                )
            )
            outgoing.state = "accepted"
            self._sync_advert()
            return {"ok": True}

    # ------------------------------------------------------------ answerer side

    def _live_incoming(self) -> list[IncomingRequest]:
        now = self._clock()
        for request_id in [rid for rid, r in self._incoming.items() if now - r.received > REQUEST_TTL_S]:
            del self._incoming[request_id]
        return sorted(self._incoming.values(), key=lambda r: r.received)

    def incoming(self) -> dict[str, Any]:
        """Requests waiting here. Never starts the listing: the app polls this for the Accept prompt."""
        with self._lock:
            record = self.pairing.record
            now = self._clock()
            return {
                "accepting": self.accepting,
                "requests": [
                    {
                        "id": r.id,
                        "from_id": r.device_id,
                        "from_label": r.label,
                        "source_ip": r.source_ip,
                        "received_at": r.received_at,
                        "expires_in_s": round(max(0.0, r.received + REQUEST_TTL_S - now), 1),
                        "replaces_peer_label": record.peer_label if record is not None else None,
                    }
                    for r in self._live_incoming()
                ],
            }

    def _take_incoming(self, request_id: str) -> IncomingRequest:
        with self._lock:
            self._live_incoming()
            request = self._incoming.pop(request_id, None)
        if request is None:
            raise PairingError("no_request", "No such B2B request (answered, cancelled, or older than 60 s)", 404)
        return request

    def accept(self, request_id: str) -> dict[str, Any]:
        request = self._take_incoming(request_id)
        token = secrets.token_urlsafe(32)
        sealed = seal_token(
            X25519PrivateKey.generate(),
            request.pub,
            request.id,
            {"token": token, "device_id": self.pairing.device_id, "label": self.pairing.label},
        )
        door_port = self.pairing.open_door()
        _post(
            f"http://{request.source_ip}:{request.door_port}{ANSWER}",
            {"request_id": request.id, "accepted": True, **sealed},
        )
        self.pairing.adopt(
            PairingRecord(
                peer_device_id=request.device_id,
                peer_label=request.label,
                peer_host=request.source_ip,
                peer_door_port=request.door_port,
                token=token,
                door_port=door_port,
                paired_at=_now_iso(),
                role="inviter",
            )
        )
        with self._lock:
            self._sync_advert()
        return {"accepted": True, "peer_label": request.label}

    def decline(self, request_id: str) -> dict[str, Any]:
        request = self._take_incoming(request_id)
        try:
            _post(
                f"http://{request.source_ip}:{request.door_port}{ANSWER}", {"request_id": request.id, "accepted": False}
            )
            told = True
        except PairingError:
            told = False
        return {"declined": True, "asker_told": told, "from_label": request.label}

    def _receive_request(self, body: dict[str, Any], source_ip: str) -> dict[str, Any]:
        if str(body.get("v")) != NEARBY_VERSION:
            raise PairingError(
                "bad_version", f"nearby protocol {body.get('v')!r}, this laptop speaks {NEARBY_VERSION}", 400
            )
        pub = _unb64(body["pub"])
        if len(pub) != 32:
            raise PairingError("bad_message", "pub must be a 32-byte X25519 key")
        door_port = int(body["door_port"])
        if not 0 < door_port < 65536:
            raise PairingError("bad_message", "door_port out of range")
        with self._lock:
            if not self.accepting:
                raise PairingError("not_accepting", "This laptop is not accepting B2B requests", 403)
            now = self._clock()
            recent = self._recent.setdefault(source_ip, deque())
            while recent and now - recent[0] > RATE_WINDOW_S:
                recent.popleft()
            if len(recent) >= RATE_PER_SOURCE:
                raise PairingError("rate_limited", f"At most {RATE_PER_SOURCE} requests a minute from one laptop", 429)
            live = self._live_incoming()
            if any(r.source_ip == source_ip for r in live):
                raise PairingError("request_pending", "A request from this laptop is already waiting", 429)
            if len(live) >= MAX_PENDING:
                raise PairingError("busy", "Too many B2B requests are waiting on this laptop", 429)
            recent.append(now)
            request = IncomingRequest(
                id=secrets.token_urlsafe(16),
                device_id=str(body["device_id"])[:64],
                label=str(body["label"]).strip()[:LABEL_MAX] or "Open DJ",
                source_ip=source_ip,
                door_port=door_port,
                pub=pub,
                received=now,
                received_at=_now_iso(),
            )
            self._incoming[request.id] = request
            return {"request_id": request.id, "expires_in_s": REQUEST_TTL_S}

    def _receive_cancel(self, body: dict[str, Any], source_ip: str) -> dict[str, Any]:
        with self._lock:
            request = self._incoming.get(str(body["request_id"]))
            if request is not None and request.source_ip == source_ip:
                del self._incoming[request.id]
        return {"ok": True}

    # ------------------------------------------------------------ reconnect

    def reconnect(self, peer_id: str) -> dict[str, Any]:
        self.start()
        if self.pairing.record is None:
            raise PairingError("not_paired", "This laptop remembers no partner: use Ask to B2B or a code", 409)
        peer = self._peer(peer_id)
        if not peer.remembered:
            raise PairingError("not_remembered", f"{peer.label} is not the partner this laptop remembers", 409)
        if not peer.addresses:
            raise PairingError("peer_unreachable", f"{peer.label} has no LAN address (bridge, wired or Wi-Fi)", 502)
        for host, kind in peer.addresses:
            try:
                return self.prove_at(host, peer.port, kind, peer.label)
            except PairingError as exc:
                if exc.code != "peer_unreachable":
                    raise  # the first address that answers at all decides, as before
        raise PairingError(
            "peer_unreachable", f"{peer.label} did not answer on any of {len(peer.addresses)} addresses", 502
        )

    def prove_at(
        self, host: str, port: int, path: str | None, label: str, timeout_s: float = DOOR_TIMEOUT_S
    ) -> dict[str, Any]:
        """B2BNEAR-04 / B2BREADDR-03: prove the shared token both ways with the door at
        ``host:port`` and, only then, adopt that address. Refusals: ``peer_unreachable`` when
        nobody answered there, ``not_remembered`` (or the door's own code) when somebody did and
        could not prove the pairing. The record is untouched by every refusal."""
        record = self.pairing.record
        if record is None:
            raise PairingError("not_paired", "This laptop remembers no partner: use Ask to B2B or a code", 409)
        door_port = self.pairing.open_door()
        base = f"http://{host}:{port}"
        challenge = _get(f"{base}{CHALLENGE}", timeout_s)
        if not isinstance(challenge.get("nonce"), str) or not isinstance(challenge.get("device_id"), str):
            raise PairingError("peer_error", f"{label} answered the challenge request with no challenge", 502)
        nonce, challenger = challenge["nonce"], challenge["device_id"]
        said = (nonce, challenger, f"{host}:{port}", self.pairing.device_id, door_port)
        body = {
            "nonce": nonce,
            "device_id": self.pairing.device_id,
            "label": self.pairing.label,
            "door_port": door_port,
            "proof": reconnect_proof(record.token, b"asker", *said),
        }
        answer = _post(f"{base}{RECONNECT}", body, timeout_s)
        expected = reconnect_proof(record.token, b"answerer", *said)
        if not hmac.compare_digest(str(answer.get("proof", "")).encode(), expected.encode()):
            raise PairingError("not_remembered", f"{label} could not prove it holds this pairing", 403)
        held = self.pairing.record
        if held is None or held.token != record.token:
            # Forget (or a new pairing) while the proof was on the wire: it wins, nothing is adopted.
            raise PairingError("pairing_changed", "This pairing was forgotten or replaced while reconnecting", 409)
        self.pairing.adopt(
            replace(
                held,
                peer_device_id=challenger[:64],
                peer_label=str(answer.get("label") or label)[:LABEL_MAX],
                peer_host=host,
                peer_door_port=port,
                door_port=door_port,
                selected_path=path or record.selected_path,
            )
        )
        return {"reconnected": True, "peer_label": self.pairing.record.peer_label}

    def _issue_challenge(self) -> dict[str, Any]:
        with self._lock:
            now = self._clock()
            self._challenges = {n: t for n, t in self._challenges.items() if now - t <= CHALLENGE_TTL_S}
            if len(self._challenges) >= MAX_CHALLENGES:
                raise PairingError("busy", "Too many reconnects in flight", 429)
            nonce = secrets.token_urlsafe(16)
            self._challenges[nonce] = now
            return {"nonce": nonce, "device_id": self.pairing.device_id}

    def _receive_reconnect(self, body: dict[str, Any], source_ip: str, local: str | None) -> dict[str, Any]:
        """``local`` is this door as it was dialed (``host:port``, read off the accepted
        connection). A transport that cannot name it (the internet door) is refused: the proof
        is checked against the dialed door, so a relayed one never matches (B2BREADDR-03)."""
        nonce = str(body["nonce"])
        asker_id = str(body["device_id"])[:64]
        door_port = int(body["door_port"])
        if not 0 < door_port < 65536:
            raise PairingError("bad_message", "door_port out of range")
        with self._lock:
            issued = self._challenges.pop(nonce, None)
        if issued is None or self._clock() - issued > CHALLENGE_TTL_S:
            raise PairingError("bad_challenge", "Unknown or stale reconnect challenge", 403)
        if local is None:
            raise PairingError("lan_only", "Reconnect is answered on the LAN door only", 403)
        record = self.pairing.record
        said = (nonce, self.pairing.device_id, local, asker_id, door_port)
        expected = None if record is None else reconnect_proof(record.token, b"asker", *said)
        if (
            record is None
            or expected is None
            or not hmac.compare_digest(str(body["proof"]).encode(), expected.encode())
        ):
            raise PairingError("not_remembered", "This laptop does not remember you: ask to B2B again", 403)
        self.pairing.adopt(
            replace(
                record,
                peer_device_id=asker_id,
                peer_label=str(body["label"]).strip()[:LABEL_MAX] or record.peer_label,
                peer_host=source_ip,
                peer_door_port=door_port,
            )
        )
        return {
            "proof": reconnect_proof(record.token, b"answerer", *said),
            "label": self.pairing.label,
        }

    # ------------------------------------------------------------ door entry points

    def door_get(self, path: str) -> dict[str, Any]:
        if path == CHALLENGE:
            return self._issue_challenge()
        raise PairingError("not_found", "not served on the B2B door", 404)

    def door_post(self, path: str, body: dict[str, Any], source_ip: str, local: str | None = None) -> dict[str, Any]:
        handlers: dict[str, Callable[[], dict[str, Any]]] = {
            REQUEST: lambda: self._receive_request(body, source_ip),
            CANCEL: lambda: self._receive_cancel(body, source_ip),
            ANSWER: lambda: self._receive_answer(body),
            RECONNECT: lambda: self._receive_reconnect(body, source_ip, local),
        }
        if path in handlers:
            return handlers[path]()
        raise PairingError("not_found", "not served on the B2B door", 404)


# ---------------------------------------------------------------- HTTP to the other door


def _answer(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise PairingError("peer_error", f"The other laptop answered {response.status_code}, not JSON", 502) from exc
    if response.status_code != 200:
        detail = payload.get("detail") if isinstance(payload, dict) else None
        if isinstance(detail, dict) and "code" in detail:
            raise PairingError(str(detail["code"]), str(detail.get("message", "")), response.status_code)
        raise PairingError("peer_error", f"The other laptop answered {response.status_code}", 502)
    if not isinstance(payload, dict):
        raise PairingError("peer_error", "The other laptop answered a non-object", 502)
    return payload


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


def _post(url: str, body: dict[str, Any], timeout_s: float = DOOR_TIMEOUT_S) -> dict[str, Any]:
    try:
        response = httpx.post(url, json=body, timeout=timeout_s)
    except httpx.HTTPError as exc:
        raise PairingError("peer_unreachable", f"The other laptop did not answer: {type(exc).__name__}", 502) from exc
    return _answer(response)


def _get(url: str, timeout_s: float = DOOR_TIMEOUT_S) -> dict[str, Any]:
    try:
        response = httpx.get(url, timeout=timeout_s)
    except httpx.HTTPError as exc:
        raise PairingError("peer_unreachable", f"The other laptop did not answer: {type(exc).__name__}", 502) from exc
    return _answer(response)


def _first(peer: NearbyPeer, call: Callable[[str], httpx.Response]) -> tuple[dict[str, Any], str, str]:
    """The first ranked address that answers at all; its answer, address and path kind."""
    if not peer.addresses:
        raise PairingError("peer_unreachable", f"{peer.label} has no LAN address (bridge, wired or Wi-Fi)", 502)
    for ip, kind in peer.addresses:
        try:
            response = call(ip)
        except httpx.HTTPError:
            continue
        return _answer(response), ip, kind
    raise PairingError(
        "peer_unreachable", f"{peer.label} did not answer on any of {len(peer.addresses)} addresses", 502
    )


def _post_first(peer: NearbyPeer, path: str, body: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
    return _first(peer, lambda ip: httpx.post(f"http://{ip}:{peer.port}{path}", json=body, timeout=DOOR_TIMEOUT_S))
