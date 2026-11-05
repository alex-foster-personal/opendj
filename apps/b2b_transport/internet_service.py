"""B2BNET-02..05: the engine's internet pairing, one per engine (held on ``app.state``).

Beside the LAN ``PairingService``, never instead of it: the LAN invite, door and record are
untouched. This owns the open internet invite, the persisted internet pairing
(``<state dir>/b2b-internet-pairing.json``, 0600) and the sealed door channel. Requests that
arrive over the channel are answered by the LAN door's own handlers (``door.serve_door_request``)
with the internet token standing in for the LAN one.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from apps.b2b_pairing.b2b_pairing import PairingError
from apps.b2b_pairing.b2b_service import PairingService
from apps.b2b_transport.channel import InternetChannel
from apps.b2b_transport.door import DoorAnswer, DoorRequest, Method, serve_door_request
from apps.b2b_transport.internet import (
    INTERNET_PAIRING_FILE,
    DoorChannel,
    Hello,
    InternetInvite,
    InternetPairing,
    join_over_relay,
    load_internet_pairing,
    relay_url,
    save_internet_pairing,
    with_peer,
)

#: How long ``accept`` waits for the first sealed session before answering (the pairing itself is done).
FIRST_SESSION_WAIT_S = 10.0

InternetState = Literal["unconfigured", "idle", "inviting", "expired", "burned", "connecting",
                        "waiting_for_partner", "connected", "relay_unreachable"]


def derive_state(status: dict[str, Any]) -> tuple[InternetState, str | None]:
    """B2BNET-12: the one named state the panel, the CLI and agents read, and why (None = nothing to add).

    A live sealed session wins over any recorded error (``last_error`` is history); a pairing with
    no relay socket and an error is ``relay_unreachable``; no relay URL at all is ``unconfigured``.
    """
    if status.get("paired"):
        return _paired_state(status)
    if status.get("relay_url") is None:
        return "unconfigured", str(status.get("relay_error"))
    return _invite_state(status.get("invite"))


def _paired_state(status: dict[str, Any]) -> tuple[InternetState, str | None]:
    channel = status.get("channel")
    if channel is None:
        restore = status.get("restore_error")
        return ("relay_unreachable", str(restore)) if restore else ("connecting", None)
    if channel.get("connected"):
        return "connected", None
    if channel.get("relay_connected"):
        return "waiting_for_partner", f"{status.get('peer_label') or 'The other laptop'} is not on the relay yet"
    if channel.get("last_error"):
        return "relay_unreachable", str(channel["last_error"])
    return "connecting", None


#: Invite state -> (named state, whether the invite's error is the reason).
_INVITE_STATES: dict[str, InternetState] = {"open": "inviting", "expired": "expired", "burned": "burned",
                                            "relay_lost": "relay_unreachable"}


def _invite_state(invite: dict[str, Any] | None) -> tuple[InternetState, str | None]:
    named = None if invite is None else _INVITE_STATES.get(str(invite.get("state")))
    if invite is None or named is None:
        return "idle", None  # no invite, or one that was used or cancelled
    if named == "inviting":
        return named, None
    if named == "expired":
        return named, f"the relay closed the invite: {invite.get('error')}"
    return named, str(invite.get("error"))


class _InternetHost:
    """The LAN door's ``PairingHost`` view of this engine, keyed by the internet token."""

    def __init__(self, lan: PairingService, token: str) -> None:
        self.device_id = lan.device_id
        self.label = lan.label
        self.nearby = None  # nearby is LAN discovery; it has no meaning across the internet
        self.relay = lan.relay
        self.booth = lan.booth
        self._token = token

    def paired_token(self) -> str | None:
        return self._token

    def invite_open(self) -> bool:
        return False

    def pair_start(self, _body: dict[str, Any], _source_ip: str) -> dict[str, Any]:
        raise PairingError("not_over_internet", "pairing legs travel through the relay rendezvous, not the door", 404)

    def pair_finish(self, _body: dict[str, Any]) -> dict[str, Any]:
        raise PairingError("not_over_internet", "pairing legs travel through the relay rendezvous, not the door", 404)


class InternetService:
    def __init__(self, state_dir: Path, lan: PairingService, engine_url: Callable[[], str]) -> None:
        self.path = state_dir / INTERNET_PAIRING_FILE
        self._lan = lan
        self._engine_url = engine_url
        self._lock = threading.RLock()
        self._invite: InternetInvite | None = None
        self._channel: DoorChannel | None = None
        self.pairing: InternetPairing | None = load_internet_pairing(self.path)
        self.restore_error: str | None = None

    # ------------------------------------------------------------ status

    def status(self) -> dict[str, Any]:
        try:
            configured, config_error = relay_url(), None
        except PairingError as exc:
            configured, config_error = None, str(exc)
        with self._lock:
            pairing, invite, channel = self.pairing, self._invite, self._channel
        status = {
            "relay_url": configured,
            "relay_error": config_error,
            "device_id": self._lan.device_id,
            "paired": pairing is not None,
            "role": None if pairing is None else pairing.role,
            "peer_label": None if pairing is None else (pairing.peer_label or None),
            "paired_at": None if pairing is None else pairing.paired_at,
            "invite": None if invite is None else invite.status(),
            "channel": None if channel is None else channel.status(),
            "restore_error": self.restore_error,
        }
        status["state"], status["state_reason"] = derive_state(status)
        return status

    # ------------------------------------------------------------ pairing

    def create_invite(self) -> dict[str, Any]:
        base = relay_url()
        with self._lock:
            if self.pairing is not None:
                raise PairingError("already_paired", "Already paired over the internet; forget that partner first", 409)
            if self._invite is not None:
                self._invite.cancel()
            self._invite = InternetInvite(base, self._lan.device_id, self._lan.label, self._on_paired)
            return self._invite.status()

    def cancel_invite(self) -> dict[str, Any]:
        with self._lock:
            invite, self._invite = self._invite, None
        if invite is not None:
            invite.cancel()
        return self.status()

    def accept(self, code: str) -> dict[str, Any]:
        base = relay_url()
        with self._lock:
            if self.pairing is not None:
                raise PairingError("already_paired", "Already paired over the internet; forget that partner first", 409)
            invite = self._invite
        if invite is not None and invite.code.replace("-", "").upper() == code.replace("-", "").strip().upper():
            raise PairingError("own_invite_code", "That's your own code. Send it to the other DJ.", 409)
        pairing = join_over_relay(base, code)
        self._adopt(pairing)
        channel = self._channel
        if channel is not None:
            channel.wait_connected(FIRST_SESSION_WAIT_S)
        return self.status()

    def _on_paired(self, pairing: InternetPairing) -> None:
        """Inviter side, from the invite's thread: the joiner opened the sealed token."""
        self._adopt(pairing)

    def _adopt(self, pairing: InternetPairing) -> None:
        with self._lock:
            save_internet_pairing(self.path, pairing)
            self.pairing = pairing
            self._start_channel(relay_url())

    def forget(self) -> dict[str, Any]:
        with self._lock:
            channel, self._channel = self._channel, None
            invite, self._invite = self._invite, None
            self.pairing = None
            self.path.unlink(missing_ok=True)
        if channel is not None:
            channel.stop()
        if invite is not None:
            invite.cancel()
        return self.status()

    # ------------------------------------------------------------ the channel

    def _start_channel(self, base: str) -> None:
        pairing = self.pairing
        assert pairing is not None, "a channel needs a pairing"
        if self._channel is not None:
            self._channel.stop()
        hello = Hello(device_id=self._lan.device_id, label=self._lan.label)
        self._channel = DoorChannel(base, pairing, self._serve, hello, self._on_hello).start()

    def _serve(self, request: DoorRequest) -> DoorAnswer:
        pairing = self.pairing
        if pairing is None:
            return DoorAnswer(401, "application/json", b'{"detail": "not paired over the internet"}')
        return serve_door_request(_InternetHost(self._lan, pairing.token), self._engine_url(), pairing.token, request)

    def _on_hello(self, hello: Hello) -> None:
        with self._lock:
            if self.pairing is not None and (self.pairing.peer_label, self.pairing.peer_device_id) != (hello.label, hello.device_id):
                self.pairing = with_peer(self.pairing, hello)
                save_internet_pairing(self.path, self.pairing)

    def request(self, request: DoorRequest, timeout: float) -> DoorAnswer:
        """One door message to the partner over the internet channel; their door's answer, raw."""
        with self._lock:
            channel = self._channel
        if channel is None:
            raise PairingError("not_paired", "Not paired over the internet", 409)
        return channel.request(request, timeout)

    def paired_channel(self) -> InternetChannel | None:
        """B2BNET-14: this pairing as a ``PairedChannel`` for peer reads and the Link relay; None unpaired."""
        with self._lock:
            pairing, channel = self.pairing, self._channel
        if pairing is None or channel is None:
            return None
        return InternetChannel(send=self.request, token=pairing.token)

    def door(self, method: Method, path: str, query: str, body: Any, timeout: float) -> dict[str, Any]:
        """Send one door message to the partner over the internet channel; their door's answer back."""
        raw = b"" if body is None else json.dumps(body).encode()
        answer = self.request(DoorRequest(method=method, path=path, query=query, body=raw), timeout)
        try:
            parsed: Any = answer.json()
        except ValueError:
            parsed = answer.body.decode(errors="replace")
        return {"status": answer.status, "content_type": answer.content_type, "body": parsed}

    # ------------------------------------------------------------ boot and quit

    def restore(self) -> None:
        """Engine start: reopen the channel for a saved pairing, or say why not (never silently)."""
        with self._lock:
            if self.pairing is None or self._channel is not None:
                return
            try:
                self._start_channel(relay_url())
                self.restore_error = None
            except PairingError as exc:
                self.restore_error = f"{exc.code}: {exc}"

    def shutdown(self) -> None:
        with self._lock:
            channel, self._channel = self._channel, None
            invite, self._invite = self._invite, None
        if channel is not None:
            channel.stop()
        if invite is not None:
            invite.cancel()
