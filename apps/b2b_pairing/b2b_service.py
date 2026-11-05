"""B2BPAIR-01..03: the engine-side pairing service, one per engine (held on ``app.state``).

Owns the open invite and its Bonjour advertisement, the LAN door, the persisted pairing
(``<state dir>/b2b-pairing.json``, 0600) and the peer config it feeds to the PEER row,
shouts and LINK PATHS (``app.state.b2b_peer_config``, ``source: "paired"``).

Routes call it (``routes/b2b_pairing.py``); the door calls its ``pair_start`` /
``pair_finish`` from its own thread, so every mutation takes ``self._lock``.
"""

from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from apps.ableton_link.link_transports import classify_interface
from apps.b2b_pairing import b2b_mdns
from apps.b2b_pairing.b2b_door import FINISH, HELLO, START, Door
from apps.b2b_pairing.b2b_pairing import (
    PAIRING_FILE,
    PROTOCOL_VERSION,
    Invite,
    JoinerHandshake,
    PairingError,
    PairingRecord,
    forget_record,
    load_record,
    normalize_code,
    rank_addresses,
    save_record,
)
from apps.b2b_pairing.b2b_readdress import idle_status

DOOR_PORT_ENV = "MDT_B2B_DOOR_PORT"
HELLO_TIMEOUT_S = 0.6
LEG_TIMEOUT_S = 3.0
PEER_SOURCE = "paired"
OWN_INVITE_CODE_MESSAGE = "That's your own code. Send it to the other DJ, or paste the code they give you."


class PairingService:
    def __init__(
        self,
        state_dir: Path,
        engine_port: Callable[[], int],
        label: str,
        set_peer: Callable[[dict[str, Any] | None], None],
        hardware_ports: Callable[[], dict[str, str]],
    ) -> None:
        self.path = state_dir / PAIRING_FILE
        self.device_id = uuid.uuid4().hex
        self.label = label
        self._engine_port = engine_port
        self._set_peer = set_peer
        self._hardware_ports = hardware_ports
        self._lock = threading.RLock()
        self._invite: Invite | None = None
        self._advertiser: b2b_mdns.Advertiser | None = None
        self._door: Door | None = None
        self.door_error: str | None = None
        self.record: PairingRecord | None = load_record(self.path)
        #: B2BNEAR-02: the nearby service (``b2b_nearby``) answers the door's ``/b2b-nearby/*``
        #: paths and keeps the door open while it advertises; None until it starts.
        self.nearby: Any = None
        #: B2BRELAY-01: the Link relay (``link_relay``) answers the door's ``/b2b-relay/*``; None until it starts.
        self.relay: Any = None
        #: B2BBOOTH-91: the shared booth the door hands booth lane POSTs to (set by the web UI layer).
        self.booth: Any = None
        #: B2BREADDR-01: the peer watch (``b2b_readdress.PeerWatch``) is told of every adopt and forget; None until it exists.
        self.watch: Any = None

    # ------------------------------------------------------------ door host protocol

    def paired_token(self) -> str | None:
        record = self.record
        return None if record is None else record.token

    def invite_open(self) -> bool:
        invite = self._invite
        return invite is not None and invite.is_open()

    def pair_start(self, body: dict[str, Any], source_ip: str) -> dict[str, Any]:
        invite = self._open_invite_or_refuse()
        joiner = {"device_id": str(body["device_id"]), "label": str(body["label"])[:64],
                  "door_port": int(body["door_port"])}
        return invite.start(str(body["msg_a"]), joiner, source_ip)

    def pair_finish(self, body: dict[str, Any]) -> dict[str, Any]:
        invite = self._open_invite_or_refuse()
        try:
            sealed = invite.finish(str(body["session"]), str(body["confirm_a"]))
        finally:
            if not invite.is_open():
                self._stop_advertising()
        paired = invite.paired
        assert paired is not None
        self._adopt(PairingRecord(
            peer_device_id=paired.peer_device_id, peer_label=paired.peer_label,
            peer_host=paired.peer_host, peer_door_port=paired.peer_door_port, token=paired.token,
            door_port=self._door_port_or_raise(), paired_at=_now(), role="inviter",
        ))
        with self._lock:
            self._invite = None
        return sealed

    # ------------------------------------------------------------ lifecycle

    def restore(self) -> None:
        """Engine boot: a saved pairing reopens its door on the same port and feeds the peer config."""
        record = self.record
        if record is None:
            return
        try:
            self._ensure_door(record.door_port)
        except OSError as exc:
            self.door_error = f"door port {record.door_port} busy: {exc}"
        self._set_peer({"url": record.peer_url, "token": record.token, "source": PEER_SOURCE})

    def shutdown(self) -> None:
        with self._lock:
            self._stop_advertising()
            if self._door is not None:
                self._door.stop()
                self._door = None

    # ------------------------------------------------------------ inviter

    def create_invite(self) -> dict[str, Any]:
        _require_mdns()
        with self._lock:
            self._stop_advertising()
            door_port = self._ensure_door(self.record.door_port if self.record else _configured_door_port())
            self._invite = Invite.create(device_id=self.device_id, label=self.label)
            try:
                self._advertiser = b2b_mdns.Advertiser(
                    f"Open DJ {self.label} {self.device_id[:6]}", door_port,
                    {"v": PROTOCOL_VERSION, "id": self.device_id},
                )
            except b2b_mdns.MdnsUnavailable as exc:
                self._invite = None
                raise PairingError("mdns_unavailable", str(exc), 503) from exc
            status = self._invite_status()
            assert status is not None
            return status

    def cancel_invite(self) -> dict[str, Any]:
        with self._lock:
            if self._invite is not None:
                self._invite.cancelled = True
            self._invite = None
            self._stop_advertising()
            self._close_door_if_idle()
        return self.status()

    # ------------------------------------------------------------ joiner

    def accept(self, typed_code: str) -> dict[str, Any]:
        code = normalize_code(typed_code)  # 422 before any network work
        self._refuse_own_invite_code(code)
        _require_mdns()
        with self._lock:
            door_port = self._ensure_door(self.record.door_port if self.record else _configured_door_port())
        try:
            return self._accept_over_lan(typed_code, door_port)
        finally:
            with self._lock:
                self._close_door_if_idle()

    def _refuse_own_invite_code(self, code: str) -> None:
        """B2BPAIR-07: the code this laptop is showing is for the OTHER DJ to type. Typed here it
        can never pair (accept skips its own device on Bonjour), so say so before any network work
        and without spending one of the invite's tries."""
        invite = self._invite
        if invite is not None and normalize_code(invite.code) == code:
            raise PairingError("own_invite_code", OWN_INVITE_CODE_MESSAGE, 409)

    def _accept_over_lan(self, typed_code: str, door_port: int) -> dict[str, Any]:
        try:
            scanned = b2b_mdns.scan()
        except b2b_mdns.MdnsUnavailable as exc:
            raise PairingError("mdns_unavailable", str(exc), 503) from exc
        candidates = [r for r in scanned.resolved if r.txt.get("id") != self.device_id]
        if not candidates:
            incomplete = f" (the search was incomplete: {'; '.join(scanned.notes)})" if scanned.partial else ""
            raise PairingError("no_inviter_found", f"No Open DJ with an open invite was found on this network{incomplete}", 404)
        last_error: PairingError | None = None
        for found in candidates:
            hardware = self._hardware_ports()
            ranked = rank_addresses([(ip, classify_interface(dev, hardware.get(dev))) for ip, dev in found.addresses])
            chosen = _first_open_inviter(ranked, found.port)
            if chosen is None:
                continue
            try:
                return self._join(chosen[0], found.port, typed_code, door_port, chosen[1])
            except PairingError as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise PairingError("no_inviter_found", f"Found {len(candidates)} Open DJ on this network, none with an open invite", 404)

    def _join(self, host: str, port: int, typed_code: str, door_port: int, path_kind: str) -> dict[str, Any]:
        base = f"http://{host}:{port}"
        handshake = JoinerHandshake(typed_code)
        start = _post(f"{base}{START}", {"msg_a": handshake.message(), "device_id": self.device_id,
                                         "label": self.label, "door_port": door_port})
        confirm_a = handshake.check_inviter(start["msg_b"], start["confirm_b"])
        sealed = _post(f"{base}{FINISH}", {"session": start["session"], "confirm_a": confirm_a})
        outcome = handshake.open_sealed(sealed)
        self._adopt(PairingRecord(
            peer_device_id=outcome.inviter_device_id, peer_label=outcome.inviter_label, peer_host=host,
            peer_door_port=port, token=outcome.token, door_port=door_port, paired_at=_now(), role="joiner",
            selected_path=path_kind,
        ))
        return self.status()

    # ------------------------------------------------------------ forget / status

    def forget(self) -> dict[str, Any]:
        with self._lock:
            had = self.record is not None
            forget_record(self.path)
            self.record = None
            if had:
                self._set_peer(None)
            self._close_door_if_idle()
        self._tell_nearby()
        return self.status()

    def status(self) -> dict[str, Any]:
        record = self.record
        return {
            # M-72: this engine's pairing id (fresh each engine start), the take-over tie-break key.
            "device_id": self.device_id,
            "paired": record is not None,
            "peer_label": record.peer_label if record else None,
            "role": record.role if record else None,
            "paired_at": record.paired_at if record else None,
            "peer_url": record.peer_url if record else None,
            "selected_path": record.selected_path if record else None,
            "door": {"running": self._door is not None, "port": self._door.port if self._door else None,
                     "error": self.door_error},
            "invite": self._invite_status(),
            "mdns_available": b2b_mdns.available(),
            # B2BREADDR-05: is the partner answering where the record says, being searched for, or found elsewhere.
            **(self.watch.status() if self.watch is not None else idle_status()),
        }

    # ------------------------------------------------------------ internals

    def _invite_status(self) -> dict[str, Any] | None:
        invite = self._invite
        if invite is None:
            return None
        return {"code": invite.code, "open": invite.is_open(), "attempts_left": invite.attempts_left}

    def _open_invite_or_refuse(self) -> Invite:
        invite = self._invite
        if invite is None:
            raise PairingError("invite_closed", "No invite is open on this laptop", 410)
        return invite

    def _adopt(self, record: PairingRecord) -> None:
        with self._lock:
            save_record(self.path, record)
            self.record = record
            self._set_peer({"url": record.peer_url, "token": record.token, "source": PEER_SOURCE})
        self._tell_nearby()

    def _tell_nearby(self) -> None:
        """B2BNEAR-04: the nearby listing's pairing tag follows the record (pair, reconnect, forget),
        and so does the peer watch's readout (B2BREADDR-05)."""
        if self.nearby is not None:
            self.nearby.pairing_changed()
        if self.watch is not None:
            self.watch.pairing_changed()

    def adopt(self, record: PairingRecord) -> None:
        """B2BNEAR-03/04: a pairing made by Accept or refreshed by Reconnect (``b2b_nearby``)."""
        self._adopt(record)

    def open_door(self) -> int:
        """B2BNEAR-02: the door's port, opening it on the saved or configured port if needed."""
        with self._lock:
            return self._ensure_door(self.record.door_port if self.record else _configured_door_port())

    def close_door_if_idle(self) -> None:
        with self._lock:
            self._close_door_if_idle()

    def _ensure_door(self, port: int) -> int:
        if self._door is None:
            self._door = Door(self, lambda: f"http://127.0.0.1:{self._engine_port()}", port)
            self.door_error = None
        return self._door.port

    def _door_port_or_raise(self) -> int:
        if self._door is None:
            raise PairingError("door_down", "The B2B door is not running", 500)
        return self._door.port

    def _close_door_if_idle(self) -> None:
        nearby_needs_door = self.nearby is not None and self.nearby.needs_door()
        if self._door is not None and self.record is None and self._invite is None and not nearby_needs_door:
            self._door.stop()
            self._door = None

    def _stop_advertising(self) -> None:
        if self._advertiser is not None:
            self._advertiser.stop()
            self._advertiser = None


# ---------------------------------------------------------------- helpers


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _configured_door_port() -> int:
    raw = os.environ.get(DOOR_PORT_ENV)
    return int(raw) if raw else 0


def _require_mdns() -> None:
    if not b2b_mdns.available():
        raise PairingError("mdns_unavailable", f"Bonjour ({b2b_mdns.client_name()}) is not available on this machine", 501)


def _first_open_inviter(ranked: list[tuple[str, str]], port: int) -> tuple[str, str] | None:
    """The best-path ``(address, path kind)`` whose door answers hello with an open invite."""
    for ip, kind in ranked:
        try:
            hello = httpx.get(f"http://{ip}:{port}{HELLO}", timeout=HELLO_TIMEOUT_S).json()
        except (httpx.HTTPError, ValueError):
            continue
        if hello.get("invite_open") is True:
            return ip, kind
    return None


def _post(url: str, body: dict[str, Any]) -> dict[str, Any]:
    try:
        response = httpx.post(url, json=body, timeout=LEG_TIMEOUT_S)
    except httpx.HTTPError as exc:
        raise PairingError("peer_unreachable", f"The inviting laptop stopped answering: {type(exc).__name__}", 502) from exc
    payload = response.json()
    if response.status_code != 200:
        detail = payload.get("detail") if isinstance(payload, dict) else None
        if isinstance(detail, dict) and "code" in detail:
            raise PairingError(str(detail["code"]), str(detail.get("message", "")), response.status_code)
        raise PairingError("peer_error", f"The inviting laptop answered {response.status_code}", 502)
    return payload  # type: ignore[no-any-return]
