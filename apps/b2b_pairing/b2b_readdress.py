"""B2BREADDR-01..06: a LAN pairing follows its partner to a new address, and only its partner.

ADR-NEW ((private path)). A pairing is a shared token; the address
in the record is only where the partner was last proven to be. DHCP can hand a laptop a new
address mid-set, and the record then names a dead host. One ``PeerWatch`` per engine, beside the
pairing service and the nearby service, keeps the record true without a person:

- **Probe.** Once a ``TICK_S`` it reads the partner's token-free ``/b2b-pair/hello`` at the stored
  address. Healthy means it answered with the device id the last proof was made with. The token
  is never sent to find out.
- **Announce.** When THIS laptop's own address toward the partner changes, it re-dials the
  partner at once at the address it still holds, so recovery does not wait on the partner's
  probes.
- **Search.** After ``FAILS_BEFORE_SEARCH`` missed probes it says ``searching`` and looks: first the
  stored address again (a partner that restarted has a new device id there), then the nearby
  scan (``b2b_nearby``, Bonjour) for every instance carrying this pairing's tag. A round every
  ``SEARCH_GAP_S`` for the first ``FAST_ROUNDS``, then every ``SLOW_GAP_S`` (a partner that went
  home must not cost a Bonjour browse every 2 s all night).
- **Proof.** A candidate is adopted only by the Reconnect exchange the nearby service already
  runs (``NearbyService.prove_at``): HMAC over a fresh single-use challenge, both ways, under the
  pairing token, naming the door that was dialed. The Bonjour tag only says where to ask; a host
  that copies it, or relays the real partner's answers, is counted in ``peer_refused`` and passed
  over.

It imports nothing from the session or sync packages: they read the peer config and the record
per call, so they follow the new address by themselves. Internet pairings (``b2b_transport``)
keep no ``PairingRecord`` and are not watched.
"""

from __future__ import annotations

import logging
import socket
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from time import monotonic
from typing import Any, Literal

import httpx

from apps.b2b_pairing import b2b_mdns
from apps.b2b_pairing.b2b_door import HELLO
from apps.b2b_pairing.b2b_pairing import PairingError, PairingRecord

#: Seconds between two looks at the partner.
TICK_S = 1.0
#: A hello that takes longer than this is a missed probe.
PROBE_TIMEOUT_S = 1.0
#: Missed probes in a row before the pairing says ``searching``. One lost Wi-Fi packet is not a
#: move, and neither is a partner whose engine is busy for two seconds: with 2, a loaded rig host
#: read ``searching`` for a moment right after a move (Wed 14 Oct 2026); the laptop that moved
#: announces itself anyway, so this only delays the search for a partner that went silent.
FAILS_BEFORE_SEARCH = 3
#: While searching, at most one round of dials (and one Bonjour scan) per this many seconds.
SEARCH_GAP_S = 2.0
#: After this many rounds (30 s) the partner is not just changing address: search less often.
FAST_ROUNDS = 15
SLOW_GAP_S = 10.0
#: Each dial of a candidate door (challenge, then proof) gives up after this long.
DIAL_TIMEOUT_S = 1.5
#: An announce the partner keeps refusing is given up after this many tries (it answers hello, so
#: the pairing itself works; an older build cannot check this build's proof).
ANNOUNCE_TRIES = 5
#: Outcomes of a dial that are not "this door failed the proof": nobody answered there, or the
#: pairing was forgotten or replaced while the dial was in flight.
_NOT_A_REFUSAL = frozenset({"peer_unreachable", "pairing_changed"})

PeerState = Literal["connected", "searching", "readdressed"]

log = logging.getLogger(__name__)


def local_address_toward(host: str) -> str | None:
    """The address this laptop would send from to reach ``host``; None when it has no route.

    A UDP ``connect`` sends nothing: it only asks the kernel which interface and source address
    the route picks."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((host, 9))
        return str(probe.getsockname()[0])
    except OSError:
        return None
    finally:
        probe.close()


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _address(record: PairingRecord) -> str:
    return f"{record.peer_host}:{record.peer_door_port}"


def idle_status() -> dict[str, Any]:
    """The readout of a laptop with no LAN pairing (or no watch): every field present, all null or 0."""
    return {
        "peer_state": None, "peer_address": None, "peer_previous_address": None, "peer_state_since": None,
        "peer_state_detail": None, "peer_search_s": None, "peer_recovery_s": None, "peer_search_attempts": 0,
        "peer_refused": 0,
    }


class PeerWatch:
    """Probe, announce, search, prove. ``tick`` is one step; ``start`` runs it on a thread.

    Network calls run outside ``self._lock``; the lock guards only the readout."""

    def __init__(
        self,
        pairing: Any,
        nearby: Any,
        enabled: Callable[[], bool] = lambda: True,
        clock: Callable[[], float] = monotonic,
        local_address: Callable[[str], str | None] = local_address_toward,
    ) -> None:
        self._pairing = pairing
        self._nearby = nearby
        self._enabled = enabled
        self._clock = clock
        self._local_address = local_address
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._token: str | None = None
        self._state: PeerState | None = None
        self._since: str | None = None
        self._address: str | None = None
        self._previous: str | None = None
        self._label = ""
        self._detail: str | None = None
        #: Why this laptop is not listed on Bonjour (its partner cannot find it); None when it is.
        self._listing_error: str | None = None
        self._last_ok = clock()
        self._search_started: float | None = None
        self._last_search = float("-inf")
        self._attempts = 0
        self._refused = 0
        self._recovery_s: float | None = None
        self._fails = 0
        self._local: str | None = None
        self._announce_left = 0
        pairing.watch = self

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="b2b-peer-watch", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.wait(TICK_S):
            try:
                self.tick()
            except Exception:  # the thread must outlive one bad tick; the next one says what is true
                log.exception("b2b peer watch tick failed")

    # ------------------------------------------------------------ readout

    def pairing_changed(self) -> None:
        """The pairing service adopted or forgot a record (from any thread, the door's included)."""
        self._observe(self._pairing.record)

    def _observe(self, record: PairingRecord | None) -> None:
        """Bring the readout in line with the record: unpaired, a new pairing, or a new address."""
        with self._lock:
            now = self._clock()
            if record is None:
                if self._token is not None:
                    self._reset(None, None, "")
                return
            address = _address(record)
            self._label = record.peer_label or "the other laptop"
            if record.token != self._token:
                self._reset(record.token, address, self._label)
                self._state, self._since = "connected", _now_iso()
                return
            if address != self._address:
                self._recovery_s = round(now - self._last_ok, 1)
                self._previous, self._address = self._address, address
                self._state, self._since = "readdressed", _now_iso()
                self._detail = f"Found {self._label} again at {address} (it was at {self._previous})."
                self._healthy(now)
                log.warning("b2b pairing readdressed: %s -> %s after %.1f s", self._previous, address, self._recovery_s)

    def _reset(self, token: str | None, address: str | None, label: str) -> None:
        self._token, self._address, self._label = token, address, label
        self._state = self._since = self._previous = self._detail = self._recovery_s = None
        self._attempts = self._refused = self._announce_left = 0
        self._local = None
        self._healthy(self._clock())

    def _healthy(self, now: float) -> None:
        self._last_ok, self._fails, self._search_started = now, 0, None

    def status(self) -> dict[str, Any]:
        with self._lock:
            if self._state is None:
                return idle_status()
            searching = self._search_started
            return {
                "peer_state": self._state,
                "peer_address": self._address,
                "peer_previous_address": self._previous,
                "peer_state_since": self._since,
                "peer_state_detail": self._detail or self._listing_error,
                "peer_search_s": None if searching is None else round(self._clock() - searching, 1),
                "peer_recovery_s": self._recovery_s,
                "peer_search_attempts": self._attempts,
                "peer_refused": self._refused,
            }

    # ------------------------------------------------------------ one step

    def tick(self) -> None:
        if not self._enabled():
            return
        record: PairingRecord | None = self._pairing.record
        self._observe(record)
        if record is None:
            return
        self._list_nearby()
        moved = self._note_local_address(record)
        if self._probe(record):
            with self._lock:
                self._healthy(self._clock())
                if self._state == "searching":
                    self._state, self._since, self._detail = "connected", _now_iso(), None
            if self._announce_left > 0:
                self._announce(record)
            return
        if self._pairing.record is not record:
            return  # adopted (the partner announced itself on the door) or forgotten while the probe was out
        with self._lock:
            self._fails += 1
            due = moved or self._state == "searching" or self._fails >= FAILS_BEFORE_SEARCH
        if due:
            self._search(record)

    def _list_nearby(self) -> None:
        """A paired laptop is listed (with its pairing tag) so its partner can find and re-dial it."""
        try:
            self._nearby.start()
            error = None
        except PairingError as exc:
            error = f"This laptop cannot be listed on the network, so its partner cannot find it if it moves: {exc}"
        with self._lock:
            self._listing_error = error

    def _note_local_address(self, record: PairingRecord) -> bool:
        """True on the tick this laptop's own address toward the partner changed (it moved)."""
        local = self._local_address(record.peer_host)
        with self._lock:
            moved = local is not None and self._local is not None and local != self._local
            if local is not None:
                self._local = local
            if moved:
                self._announce_left = ANNOUNCE_TRIES
        return moved

    def _probe(self, record: PairingRecord) -> bool:
        """The stored address answers hello as the device the last proof was made with."""
        try:
            response = httpx.get(f"{record.peer_url}{HELLO}", timeout=PROBE_TIMEOUT_S)
            hello = response.json()
        except (httpx.HTTPError, ValueError):
            return False
        return response.status_code == 200 and isinstance(hello, dict) and hello.get("device_id") == record.peer_device_id

    def _announce(self, record: PairingRecord) -> None:
        """This laptop moved and the partner still answers where it was: tell it the new address."""
        with self._lock:
            self._announce_left -= 1
        self._prove(record.peer_host, record.peer_door_port, record.selected_path, record.peer_label)

    def _search(self, record: PairingRecord) -> None:
        with self._lock:
            now = self._clock()
            if self._state != "searching":
                self._state, self._since, self._search_started = "searching", _now_iso(), now
                self._attempts = 0
                self._last_search = float("-inf")
                log.warning("b2b partner stopped answering at %s; searching", self._address)
            self._detail = f"Looking for {self._label} on this network. It stopped answering at {self._address}."
            if now - self._last_search < (SEARCH_GAP_S if self._attempts < FAST_ROUNDS else SLOW_GAP_S):
                return
            self._last_search = now
            self._attempts += 1
        # The stored address first: a partner that restarted is still there, under a new device id.
        if self._prove(record.peer_host, record.peer_door_port, record.selected_path, record.peer_label):
            return
        if not b2b_mdns.available():
            return
        try:
            candidates = self._nearby.find_partner()
        except PairingError as exc:
            with self._lock:
                self._detail = f"Looking for {self._label}: the network search failed ({exc})."
            return
        for peer in candidates:
            for host, kind in peer.addresses:
                if (host, peer.port) == (record.peer_host, record.peer_door_port):
                    continue  # dialed above
                if self._prove(host, peer.port, kind, peer.label):
                    return

    def _prove(self, host: str, port: int, path: str | None, label: str) -> bool:
        """Run the Reconnect proof at one door. True = proven and adopted (the readout follows the
        record). A door that answers but cannot prove the token is counted, never adopted."""
        try:
            self._nearby.prove_at(host, port, path, label, DIAL_TIMEOUT_S)
        except PairingError as exc:
            if exc.code not in _NOT_A_REFUSAL:
                with self._lock:
                    self._refused += 1
                log.warning("b2b door %s:%s did not prove the pairing: %s %s", host, port, exc.code, exc)
            return False
        with self._lock:
            self._healthy(self._clock())
            self._announce_left = 0  # the partner has this laptop's address from the proof itself
            if self._state == "searching":  # proven where it already was: a restart, not a move
                self._state, self._since, self._detail = "connected", _now_iso(), None
        return True
