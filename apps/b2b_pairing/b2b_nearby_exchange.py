"""Nearby ask/accept/decline mixin (split from b2b_nearby)."""

from __future__ import annotations

import secrets
from collections import deque
from typing import Any

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from apps.b2b_pairing.b2b_pairing import PairingError, PairingRecord

from .b2b_nearby_http import _post, _post_first
from .b2b_nearby_protocol import (
    ANSWER,
    CANCEL,
    LABEL_MAX,
    MAX_PENDING,
    NEARBY_VERSION,
    RATE_PER_SOURCE,
    RATE_WINDOW_S,
    REQUEST,
    REQUEST_TTL_S,
    IncomingRequest,
    OutgoingRequest,
    OutgoingState,
    _b64,
    _now_iso,
    _unb64,
    open_token,
    seal_token,
)


class NearbyExchangeMixin:
    def request(self, peer_id: str) -> dict[str, Any]:
        self.start()
        peer = self._peer(peer_id)
        if not peer.accepting:
            raise PairingError("not_accepting", f"{peer.label} is not accepting B2B requests", 409)
        peer_pub = peer.pub
        if peer_pub is None or len(peer_pub) != 32:
            raise PairingError(
                "no_peer_key", f"{peer.label} did not advertise a pairing key: update Open DJ, or use a code", 409
            )
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
                peer_pub=peer_pub,
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
                data = open_token(outgoing.private, outgoing.id, body, outgoing.peer_pub)
            except PairingError as exc:
                outgoing.state = "failed"
                outgoing.detail = exc.code
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
            self._identity,
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
