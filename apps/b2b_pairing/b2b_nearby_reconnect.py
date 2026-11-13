"""Nearby reconnect mixin (split from b2b_nearby)."""

from __future__ import annotations

import secrets
from dataclasses import replace
from typing import Any

import hmac

from apps.b2b_pairing.b2b_pairing import PairingError

from .b2b_nearby_http import _get, _post
from .b2b_nearby_protocol import (
    CHALLENGE,
    CHALLENGE_TTL_S,
    DOOR_TIMEOUT_S,
    LABEL_MAX,
    MAX_CHALLENGES,
    RECONNECT,
    _now_iso,
    reconnect_proof,
)


class NearbyReconnectMixin:
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
