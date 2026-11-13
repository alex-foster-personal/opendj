"""NearbyService: advertise, browse, ask, answer and reconnect."""

from __future__ import annotations

import json
import logging
import threading
from collections import deque
from collections.abc import Callable
from pathlib import Path
from time import monotonic
from typing import Any

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from apps.b2b_pairing import b2b_mdns
from apps.b2b_pairing.b2b_pairing import PairingError

from .b2b_nearby_exchange import NearbyExchangeMixin
from .b2b_nearby_protocol import (
    ANSWER,
    CANCEL,
    CHALLENGE,
    RECONNECT,
    REQUEST,
    SETTINGS_FILE,
    build_txt,
    pairing_tag,
)
from .b2b_nearby_reconnect import NearbyReconnectMixin
from .b2b_nearby_scan import NearbyScanMixin

log = logging.getLogger(__name__)


class NearbyService(NearbyScanMixin, NearbyExchangeMixin, NearbyReconnectMixin):
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
        self._identity = X25519PrivateKey.generate()

    def running(self) -> bool:
        return self._advertiser is not None

    def needs_door(self) -> bool:
        outgoing = self.outgoing
        return self.running() or (outgoing is not None and outgoing.state == "pending")

    def start(self) -> None:
        if not b2b_mdns.available():
            raise PairingError(
                "mdns_unavailable", f"Bonjour ({b2b_mdns.client_name()}) is not available on this machine", 501
            )
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
            self._identity.public_key().public_bytes_raw(),
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
