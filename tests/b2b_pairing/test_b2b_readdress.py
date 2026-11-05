"""B2BREADDR-01..06: a LAN pairing follows its partner to a new address, and only its partner.

Two (or three) real ``PairingService`` + ``NearbyService`` + ``PeerWatch`` stacks on an in-process
LAN (``_Lan``): every HTTP call the pairing package makes is routed by ``(ip, port)`` to the real
door functions of the laptop that holds that address (``answer_unpaired_get`` / ``answer_post``),
so the door's own admission runs, and Bonjour is the set of adverts the laptops registered. A
laptop "moves" by changing the address its door answers on. The clock is injected. The same
scenario over real sockets, real Avahi and a namespace re-IP is tests/b2b_e2e/test_peer_readdress_e2e.py.

- [if] the partner moves and neither laptop finds the other without a person [then] fail
- [if] a host that copies the pairing tag, or relays the real partner's proof, is adopted [then] fail
- [if] an unrelated device on the old address is taken for the partner [then] fail
- [if] a healthy pairing is re-dialed, or a forgotten one is searched for [then] fail
- [if] status does not say searching, then readdressed with the old and the new address [then] fail
- [if] a paired partner changes address [then] both laptops carry on at the new one, and only with the token holder, [else stop]
"""

from __future__ import annotations

import json
import socket
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest

from apps.b2b_pairing import b2b_mdns, b2b_readdress
from apps.b2b_pairing import b2b_nearby as bn
from apps.b2b_pairing.b2b_door import Door, answer_post, answer_unpaired_get
from apps.b2b_pairing.b2b_pairing import PairingError, PairingRecord
from apps.b2b_pairing.b2b_readdress import PeerWatch
from apps.b2b_pairing.b2b_service import PairingService

pytestmark = [pytest.mark.requirement(f"B2BREADDR-0{n}") for n in range(1, 7)]

TOKEN = "the-shared-pairing-token-0123456789abcdef"
AIR_OLD, AIR_NEW, SILVER, IMPOSTOR = "192.168.0.86", "192.168.0.191", "192.168.0.20", "192.168.0.66"


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@dataclass
class _Laptop:
    name: str
    ip: str | None
    pairing: PairingService
    nearby: bn.NearbyService
    watch: PeerWatch
    peer_configs: list[dict[str, Any] | None]

    @property
    def port(self) -> int:
        return int(self.pairing.open_door())

    @property
    def record(self) -> PairingRecord:
        record = self.pairing.record
        assert record is not None, f"{self.name} holds no pairing"
        return record

    @property
    def peer_address(self) -> str:
        return f"{self.record.peer_host}:{self.record.peer_door_port}"


Handler = Callable[[str, str, bytes, str, str], tuple[int, Any]]


@dataclass
class _Lan:
    """``(ip, port)`` -> who answers there. A laptop answers with its real door functions."""

    clock: _Clock
    laptops: list[_Laptop] = field(default_factory=list)
    #: Anything else on the LAN: an impostor, a printer on a recycled address.
    others: dict[tuple[str, int], Handler] = field(default_factory=dict)
    #: Extra Bonjour answers (an impostor's copied advert), listed before the real ones.
    extra_adverts: list[b2b_mdns.Resolved] = field(default_factory=list)
    mdns_up: bool = True
    caller_ip: str | None = None
    #: Every request that reached a door: (caller ip, method, ip, port, path).
    log: list[tuple[str | None, str, str, int, str]] = field(default_factory=list)
    adverts: dict[int, tuple[str, dict[str, str]]] = field(default_factory=dict)
    scans: int = 0
    #: Called with (method, path) as a request reaches a laptop's door, before it is answered.
    on_request: Callable[[str, str], None] | None = None
    #: Called once a request to an address nobody holds has been sent, before it times out.
    on_dead: Callable[[], None] | None = None

    def tick(self, laptop: _Laptop, times: int = 1, step_s: float = b2b_readdress.TICK_S) -> None:
        for _ in range(times):
            self.clock.now += step_s
            self.caller_ip = laptop.ip
            try:
                laptop.watch.tick()
            finally:
                self.caller_ip = None

    def _laptop_at(self, ip: str, port: int) -> _Laptop | None:
        for laptop in self.laptops:
            if laptop.ip == ip and laptop.pairing.status()["door"]["port"] == port:
                return laptop
        return None

    def _send(self, method: str, url: str, raw: bytes) -> httpx.Response:
        parts = urlsplit(url)
        ip, port = str(parts.hostname), int(parts.port or 80)
        source = self.caller_ip
        if source is None:
            raise httpx.ConnectError("this laptop has no address, so no route")
        self.log.append((source, method, ip, port, parts.path))
        request = httpx.Request(method, url)
        other = self.others.get((ip, port))
        if other is not None:
            status, body = other(method, parts.path, raw, source, ip)
            return httpx.Response(status, content=body if isinstance(body, bytes) else json.dumps(body).encode(), request=request)
        laptop = self._laptop_at(ip, port)
        if laptop is None:
            if self.on_dead is not None:
                hook, self.on_dead = self.on_dead, None
                hook()
            raise httpx.ConnectError(f"[Errno 64] Host is down: {ip}:{port}")
        if self.on_request is not None:
            self.on_request(method, parts.path)
        if method == "GET":
            answer = answer_unpaired_get(laptop.pairing, parts.path)
            status, body = (401, {"detail": "not paired"}) if answer is None else answer[:2]
        else:
            status, body, _ = answer_post(laptop.pairing, parts.path, raw, source, f"{ip}:{port}")
        return httpx.Response(status, content=body if isinstance(body, bytes) else json.dumps(body).encode(), request=request)

    def get(self, url: str, **_kwargs: Any) -> httpx.Response:
        return self._send("GET", url, b"")

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self._send("POST", url, json.dumps(kwargs.get("json")).encode())

    def scan(self, *_args: Any, **_kwargs: Any) -> b2b_mdns.ScanResult:
        self.scans += 1
        if not self.mdns_up:
            return b2b_mdns.ScanResult([])
        found = list(self.extra_adverts)
        for laptop in self.laptops:
            port = laptop.pairing.status()["door"]["port"]
            if laptop.ip is None or port not in self.adverts:
                continue
            instance, txt = self.adverts[port]
            found.append(b2b_mdns.Resolved(instance, f"{laptop.name}.local", port, dict(txt), [(laptop.ip, "en0")]))
        return b2b_mdns.ScanResult(found)

    def calls(self, path: str) -> int:
        return sum(1 for entry in self.log if entry[4] == path)


@pytest.fixture
def lan(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Lan]:
    net = _Lan(_Clock())

    class _Advert:
        def __init__(self, instance: str, port: int, txt: dict[str, str], stype: str | None = None) -> None:
            self.port = port
            net.adverts[port] = (instance, dict(txt))

        def alive(self) -> bool:
            return self.port in net.adverts

        def stop(self) -> None:
            net.adverts.pop(self.port, None)

    monkeypatch.setattr(b2b_mdns, "available", lambda: True)
    monkeypatch.setattr(b2b_mdns, "Advertiser", _Advert)
    monkeypatch.setattr(b2b_mdns, "scan", net.scan)
    monkeypatch.setattr(httpx, "get", net.get)
    monkeypatch.setattr(httpx, "post", net.post)
    yield net
    for laptop in net.laptops:
        laptop.nearby.stop()
        laptop.pairing.shutdown()


def _laptop(lan: _Lan, root: Path, name: str, ip: str) -> _Laptop:
    state = root / name
    configs: list[dict[str, Any] | None] = []
    pairing = PairingService(state, engine_port=lambda: 1, label=name, set_peer=configs.append,
                             hardware_ports=lambda: {"en0": "Wi-Fi"})
    nearby = bn.NearbyService(pairing, state, lambda: {"en0": "Wi-Fi"}, clock=lan.clock)
    laptop = _Laptop(name, ip, pairing, nearby, None, configs)  # type: ignore[arg-type]
    laptop.watch = PeerWatch(pairing, nearby, clock=lan.clock, local_address=lambda _host: laptop.ip)
    lan.laptops.append(laptop)
    return laptop


def _paired(lan: _Lan, tmp_path: Path) -> tuple[_Laptop, _Laptop]:
    """Air and Silver, paired as an invite code leaves them, each watch having seen one healthy tick."""
    air, silver = _laptop(lan, tmp_path, "Air", AIR_OLD), _laptop(lan, tmp_path, "Silver", SILVER)
    for me, other, role in ((air, silver, "joiner"), (silver, air, "inviter")):
        assert other.ip is not None
        me.pairing.adopt(PairingRecord(
            peer_device_id=other.pairing.device_id, peer_label=other.name, peer_host=other.ip,
            peer_door_port=other.port, token=TOKEN, door_port=me.port, paired_at="2026-10-13T22:00:00Z", role=role,  # type: ignore[arg-type]
        ))
    for laptop in (air, silver):
        lan.tick(laptop)
        assert laptop.watch.status()["peer_state"] == "connected", laptop.watch.status()
    lan.log.clear()
    lan.scans = 0
    return air, silver


def _impostor_advert(lan: _Lan, copied_from: _Laptop, port: int, device_id: str) -> None:
    """What anyone on the LAN can do: read the partner's TXT off Bonjour and advertise it again."""
    _instance, txt = lan.adverts[copied_from.port]
    lan.extra_adverts.append(b2b_mdns.Resolved("Open DJ Air copy n", "evil.local", port, {**txt, "id": device_id}, [(IMPOSTOR, "en0")]))


# ---------------------------------------------------------------- recovery


def test_the_laptop_that_moved_tells_its_partner_at_once(lan: _Lan, tmp_path: Path) -> None:
    air, silver = _paired(lan, tmp_path)
    old = silver.peer_address
    air.ip = AIR_NEW  # the DHCP lease changed under a live pairing
    lan.tick(air)  # ONE tick of the mover: it sees its own address change and re-dials
    assert silver.peer_address == f"{AIR_NEW}:{air.port}", "if the mover does not announce itself then recovery waits on the partner's timeout"
    status = silver.watch.status()
    assert status["peer_state"] == "readdressed"
    assert (status["peer_previous_address"], status["peer_address"]) == (old, f"{AIR_NEW}:{air.port}")
    assert status["peer_recovery_s"] is not None and 0 <= status["peer_recovery_s"] <= 2 * b2b_readdress.TICK_S
    assert silver.peer_configs[-1] == {"url": f"http://{AIR_NEW}:{air.port}", "token": TOKEN, "source": "paired"}, (
        "if the peer config keeps the old url then decks, shouts, partner audio and the relay keep polling a dead address")
    assert air.watch.status()["peer_state"] == "connected", "the mover's partner did not move"
    assert lan.calls(bn.RECONNECT) == 1 and lan.scans == 0, "one proof at the known address, no scan needed"


def test_a_partner_that_moved_silently_is_found_by_its_pairing_tag(lan: _Lan, tmp_path: Path) -> None:
    air, silver = _paired(lan, tmp_path)
    old = silver.peer_address
    air.ip, lan.mdns_up = AIR_NEW, False  # moved; Bonjour has not caught up yet; Air's own watch never runs
    lan.tick(silver, b2b_readdress.FAILS_BEFORE_SEARCH + 1)
    searching = silver.watch.status()
    assert searching["peer_state"] == "searching", "if a dead address still reads connected then the user is told nothing"
    assert searching["peer_address"] == old and searching["peer_previous_address"] is None
    assert searching["peer_search_s"] is not None and searching["peer_search_s"] >= 0
    assert "Air" in searching["peer_state_detail"] and old in searching["peer_state_detail"]
    assert silver.peer_address == old, "nothing is adopted while nothing is proven"
    assert lan.scans == 1 and searching["peer_search_attempts"] == 1, "one round per SEARCH_GAP_S, not one per tick"
    assert searching["peer_refused"] == 0, "nobody answering is not somebody failing the proof"
    lan.mdns_up = True
    lan.tick(silver, 3)
    found = silver.watch.status()
    assert found["peer_state"] == "readdressed", found
    assert (found["peer_previous_address"], found["peer_address"]) == (old, f"{AIR_NEW}:{air.port}")
    assert found["peer_search_s"] is None and found["peer_recovery_s"] > 0
    assert air.peer_address == f"{SILVER}:{silver.port}", "the found partner keeps (re-learns) this laptop's address"


def test_both_laptops_moving_still_find_each_other(lan: _Lan, tmp_path: Path) -> None:
    air, silver = _paired(lan, tmp_path)
    air.ip, silver.ip = AIR_NEW, "192.168.0.21"
    for _ in range(6):
        lan.tick(air)
        lan.tick(silver)
    assert air.peer_address == f"192.168.0.21:{silver.port}" and silver.peer_address == f"{AIR_NEW}:{air.port}"
    assert {air.watch.status()["peer_state"], silver.watch.status()["peer_state"]} <= {"readdressed", "connected"}
    assert air.watch.status()["peer_state"] == "readdressed" or silver.watch.status()["peer_state"] == "readdressed"


def test_a_partner_that_restarted_on_the_same_address_is_proven_again_not_readdressed(lan: _Lan, tmp_path: Path) -> None:
    air, silver = _paired(lan, tmp_path)
    air.pairing.device_id = "f" * 32  # a new engine run: a new per-boot id, same address, same token
    air.nearby.pairing_changed()
    lan.tick(silver, b2b_readdress.FAILS_BEFORE_SEARCH + 1)
    status = silver.watch.status()
    assert status["peer_state"] == "connected" and status["peer_previous_address"] is None, status
    assert silver.record.peer_device_id == "f" * 32


def test_a_laptop_with_no_network_says_so_and_recovers_when_it_is_back(lan: _Lan, tmp_path: Path) -> None:
    air, silver = _paired(lan, tmp_path)
    air.ip = None  # Wi-Fi dropped: no address at all
    lan.tick(air, b2b_readdress.FAILS_BEFORE_SEARCH + 1)
    assert air.watch.status()["peer_state"] == "searching"
    air.ip = AIR_NEW
    lan.tick(air, 2)
    assert air.watch.status()["peer_state"] == "connected", air.watch.status()
    assert silver.peer_address == f"{AIR_NEW}:{air.port}"


# ---------------------------------------------------------------- controls: no overshoot


def test_a_healthy_pairing_is_probed_and_never_re_dialed(lan: _Lan, tmp_path: Path) -> None:
    air, silver = _paired(lan, tmp_path)
    lan.tick(silver, 20)
    assert lan.calls(bn.CHALLENGE) == 0 and lan.calls(bn.RECONNECT) == 0, "if a healthy pairing is re-proven every tick then the watch is a flood"
    assert lan.calls("/b2b-pair/hello") == 20
    assert silver.watch.status()["peer_state"] == "connected" and silver.peer_configs.count(silver.peer_configs[-1]) == 1
    assert air.record.peer_host == SILVER


def test_two_missed_probes_are_not_a_search(lan: _Lan, tmp_path: Path) -> None:
    air, silver = _paired(lan, tmp_path)
    kept = air.ip
    air.ip = None  # one lost probe (Wi-Fi blip), then it answers again
    lan.tick(silver, 2)  # a literal 2: derived from the constant, a mutated constant would move the test with it
    assert silver.watch.status()["peer_state"] == "connected", "if two lost probes read as searching then a busy partner makes the card flicker"
    air.ip = kept
    lan.tick(silver, 3)
    assert silver.watch.status()["peer_state"] == "connected" and lan.calls(bn.CHALLENGE) == 0


def test_only_doors_that_carry_the_pairing_tag_are_dialed(lan: _Lan, tmp_path: Path) -> None:
    """Two other Open DJs are nearby: one unpaired (no tag), one paired with somebody else (another
    tag). Neither is asked anything while Silver looks for Air."""
    air, silver = _paired(lan, tmp_path)
    plain, other = _laptop(lan, tmp_path, "Plain", "192.168.0.30"), _laptop(lan, tmp_path, "Other", "192.168.0.31")
    assert other.ip is not None
    other.pairing.adopt(PairingRecord(peer_device_id="9" * 32, peer_label="Somebody", peer_host="192.168.0.99", peer_door_port=4999,
                                      token="another-pairing-token-0123456789abcdef", door_port=other.port, paired_at="t", role="joiner"))
    for neighbor in (plain, other):
        neighbor.nearby.start()
    air.ip = None
    lan.tick(silver, 8)
    listed = {r.port for r in lan.scan().resolved}
    assert {plain.port, other.port} <= listed, "control: both neighbors were on Bonjour for Silver to see"
    asked = {entry[2] for entry in lan.log if entry[0] == SILVER}
    assert asked == {AIR_OLD}, f"if the search dials every Open DJ nearby then strangers get this laptop's reconnects: {asked}"
    assert silver.watch.status()["peer_state"] == "searching" and silver.watch.status()["peer_refused"] == 0


def test_a_long_search_slows_down(lan: _Lan, tmp_path: Path) -> None:
    air, silver = _paired(lan, tmp_path)
    air.ip = None  # the partner went home
    fast_s = int(b2b_readdress.FAST_ROUNDS * b2b_readdress.SEARCH_GAP_S)
    lan.tick(silver, b2b_readdress.FAILS_BEFORE_SEARCH - 1 + fast_s)
    assert lan.scans == b2b_readdress.FAST_ROUNDS, "the first 30 s: one round every 2 s"
    lan.tick(silver, 30)
    assert lan.scans == b2b_readdress.FAST_ROUNDS + 3, "if the search never slows then a partner that left costs a Bonjour browse every 2 s for good"
    air.ip = AIR_NEW  # and it is still found when it comes back
    lan.tick(silver, int(b2b_readdress.SLOW_GAP_S))
    assert silver.peer_address == f"{AIR_NEW}:{air.port}", "if a slowed search stops then a partner back after a minute is never found"


@pytest.mark.parametrize("pressed", ["forget", "pair with somebody else"])
def test_forget_or_a_new_pairing_during_a_dial_wins(lan: _Lan, tmp_path: Path, pressed: str) -> None:
    """Forget is pressed (or a new partner accepted) while the proof is on the wire: the late
    answer must not bring the old pairing back, and it is not an impostor either."""
    air, silver = _paired(lan, tmp_path)
    air.ip = AIR_NEW
    newer = PairingRecord(peer_device_id="7" * 32, peer_label="Newer", peer_host="192.168.0.77", peer_door_port=4777,
                          token="a-newer-pairing-token-0123456789abcdef", door_port=silver.port, paired_at="t", role="joiner")

    def as_the_proof_arrives(method: str, path: str) -> None:
        if path == bn.RECONNECT and silver.pairing.record is not None and silver.pairing.record.token == TOKEN:
            silver.pairing.forget() if pressed == "forget" else silver.pairing.adopt(newer)

    lan.on_request = as_the_proof_arrives
    lan.tick(silver, b2b_readdress.FAILS_BEFORE_SEARCH + 1)
    assert lan.calls(bn.RECONNECT) == 1, "control: a proof was sent and answered while the pairing changed"
    status = silver.watch.status()
    assert status["peer_refused"] == 0, "the real partner answering a pairing that has just ended is not a door failing the proof"
    if pressed == "forget":
        assert silver.pairing.record is None and not silver.pairing.path.exists(), "if a late proof re-saves the record then Forget is a lie"
        assert status["peer_state"] is None and silver.peer_configs[-1] is None
    else:
        assert silver.pairing.record == newer, "if a late proof overwrites the new pairing then its token is replaced by the old partner's address"
        assert status["peer_address"] == "192.168.0.77:4777"


def test_a_partner_that_finds_this_laptop_while_its_probe_is_out_is_not_searched_for(lan: _Lan, tmp_path: Path) -> None:
    """Both moved. Air's search finds Silver and proves itself on Silver's door while Silver's own
    probe of Air's old address is still timing out. That probe is about an address the pairing
    no longer holds, so it must not start a search (seen on the rig: readdressed, then searching)."""
    air, silver = _paired(lan, tmp_path)
    air.ip, silver.ip = AIR_NEW, "192.168.0.21"

    def air_finds_silver() -> None:
        kept = lan.caller_ip
        lan.tick(air, step_s=0.0)
        lan.caller_ip = kept

    lan.on_dead = air_finds_silver
    lan.tick(silver, 1)
    status = silver.watch.status()
    assert silver.peer_address == f"{AIR_NEW}:{air.port}", "control: Air's proof landed on Silver's door during Silver's probe"
    assert status["peer_state"] == "readdressed" and status["peer_search_attempts"] == 0, (
        f"if a probe of the address just left behind starts a search then the card flickers to searching after a move: {status}")


def test_forget_ends_the_search_and_a_disabled_watch_does_nothing(lan: _Lan, tmp_path: Path) -> None:
    air, silver = _paired(lan, tmp_path)
    air.ip = None
    lan.tick(silver, b2b_readdress.FAILS_BEFORE_SEARCH + 1)
    assert silver.watch.status()["peer_state"] == "searching"
    silver.pairing.forget()
    idle = silver.watch.status()
    assert idle["peer_state"] is None and idle["peer_address"] is None and idle["peer_search_s"] is None
    lan.log.clear()
    lan.tick(silver, 5)
    assert lan.log == [], "if a forgotten partner is still searched for then Forget is a lie"
    off = PeerWatch(air.pairing, air.nearby, enabled=lambda: False, clock=lan.clock, local_address=lambda _host: AIR_NEW)
    lan.caller_ip = AIR_NEW
    off.tick()
    assert lan.log == [] and off.status()["peer_state"] is None, "the watch is gated by the Link flag"


# ---------------------------------------------------------------- security: only the token holder


def _refuser(challenger_id: str) -> Handler:
    """An impostor door: it hands out challenges and answers every reconnect with a made-up proof."""
    def handle(method: str, path: str, _raw: bytes, _source: str, _local: str) -> tuple[int, Any]:
        if path == "/b2b-pair/hello":
            return 200, {"v": "1", "device_id": challenger_id, "label": "Air", "invite_open": False}
        if path == bn.CHALLENGE:
            return 200, {"nonce": "bm9uY2U", "device_id": challenger_id}
        if path == bn.RECONNECT:
            return 200, {"proof": bn.reconnect_proof("guessed", b"answerer", "bm9uY2U", challenger_id, "x:1", "y", 1), "label": "Air"}
        return 404, {"detail": "nope"}
    return handle


@pytest.mark.parametrize("same_id", [False, True], ids=["own-id", "copied-id"])
def test_a_host_that_copies_the_pairing_tag_is_never_adopted(lan: _Lan, tmp_path: Path, same_id: bool) -> None:
    air, silver = _paired(lan, tmp_path)
    old = silver.peer_address
    evil_id = air.pairing.device_id if same_id else "e" * 32
    _impostor_advert(lan, air, 4666, evil_id)
    lan.others[(IMPOSTOR, 4666)] = _refuser(evil_id)
    real_ip, air.ip = air.ip, None  # the real partner is away: only the impostor answers to the name
    lan.tick(silver, 8)
    status = silver.watch.status()
    assert silver.peer_address == old, "if a copied Bonjour tag is enough then anyone on the Wi-Fi takes over the pairing"
    assert all(config is None or IMPOSTOR not in str(config["url"]) for config in silver.peer_configs), "the token must never be sent there"
    assert status["peer_state"] == "searching" and status["peer_refused"] >= 1, status
    assert lan.calls(bn.RECONNECT) >= 1, "control: the impostor WAS dialed and its answer checked, not merely unlisted"
    assert real_ip is not None
    air.ip = AIR_NEW  # the real partner returns elsewhere, listed AFTER the impostor
    lan.tick(silver, 3)
    assert silver.peer_address == f"{AIR_NEW}:{air.port}", "if the impostor hides the real partner then a copied tag is a lockout"
    assert silver.watch.status()["peer_state"] == "readdressed"


@pytest.mark.parametrize("bad", ["an IPv6 address", "not an address", "an empty challenge", "a challenge that is a list", "a page of HTML"])
def test_a_malformed_candidate_listed_first_does_not_hide_the_real_partner(lan: _Lan, tmp_path: Path, bad: str) -> None:
    """Seen on the rig: Avahi resolved a copied advert to an IPv6 address, the dial raised
    InvalidURL, the whole search round died and the real partner behind it was never asked."""
    air, silver = _paired(lan, tmp_path)
    _instance, txt = lan.adverts[air.port]
    address = {"an IPv6 address": "fe80::431:eff:fe3f:29f8", "not an address": "evil.local/x?"}.get(bad, IMPOSTOR)
    lan.extra_adverts.append(b2b_mdns.Resolved("Open DJ Air copy n", "evil.local", 4666, {**txt, "id": "e" * 32}, [(address, "en0")]))
    answers: dict[str, tuple[int, Any]] = {
        "an empty challenge": (200, {}),
        "a challenge that is a list": (200, [1, 2]),
        "a page of HTML": (200, b"<html>hello</html>"),
    }
    if bad in answers:
        lan.others[(IMPOSTOR, 4666)] = lambda *_a: answers[bad]
    air.ip = AIR_NEW  # the real partner moved and is listed AFTER the malformed one
    lan.tick(silver, b2b_readdress.FAILS_BEFORE_SEARCH)
    assert silver.peer_address == f"{AIR_NEW}:{air.port}", f"if {bad} ends the round then one bad advert locks the pairing out"
    assert silver.watch.status()["peer_state"] == "readdressed"


def test_a_relay_that_forwards_the_real_partners_proof_is_refused(lan: _Lan, tmp_path: Path) -> None:
    """The impostor cannot make a proof, so it borrows one: it passes Silver's challenge request and
    reconnect on to the real Air and hands back whatever Air answers. The proof names the door the
    asker dialed, so Air sees a proof for another door and refuses."""
    air, silver = _paired(lan, tmp_path)
    air.ip = AIR_NEW
    old_silver_view, old_air_view = silver.peer_address, air.peer_address

    def relay(method: str, path: str, raw: bytes, _source: str, _local: str) -> tuple[int, Any]:
        if path == "/b2b-pair/hello":
            return 404, {"detail": "nope"}
        kept, lan.caller_ip = lan.caller_ip, IMPOSTOR
        try:
            url = f"http://{AIR_NEW}:{air.port}{path}"
            response = lan.get(url) if method == "GET" else lan.post(url, json=json.loads(raw))
        finally:
            lan.caller_ip = kept
        return response.status_code, response.json()

    lan.others[(IMPOSTOR, 4666)] = relay
    _impostor_advert(lan, air, 4666, air.pairing.device_id)
    lan.adverts.pop(air.port)  # only the relay is listed: Silver's one way to "Air" is through it
    lan.tick(silver, 6)
    relayed = [entry for entry in lan.log if entry[0] == IMPOSTOR and entry[4] == bn.RECONNECT]
    assert relayed, "control: the relay did reach the real partner with Silver's proof"
    assert silver.peer_address == old_silver_view, "if a relayed proof is honored then the relay becomes the partner's address"
    assert air.peer_address == old_air_view, "if the real partner adopts the relay's address then its token goes to the relay"
    assert silver.watch.status()["peer_state"] == "searching" and silver.watch.status()["peer_refused"] >= 1


def test_an_unrelated_device_on_the_old_address_is_not_the_partner(lan: _Lan, tmp_path: Path) -> None:
    air, silver = _paired(lan, tmp_path)
    old_port = air.port
    air.ip = AIR_NEW
    # DHCP gave the old address to a printer that even listens on the door's port, and answers 200.
    lan.others[(AIR_OLD, old_port)] = lambda *_a: (200, b"<html>printer</html>")
    lan.tick(silver, 5)
    assert silver.peer_address == f"{AIR_NEW}:{air.port}", "if any answer at the old address counts as the partner then recovery never starts"
    lan.others[(AIR_OLD, old_port)] = _refuser("d" * 32)  # another Open DJ on the old address
    silver.pairing.adopt(replace(silver.record, peer_host=AIR_OLD))
    lan.tick(silver, 5)
    assert silver.peer_address == f"{AIR_NEW}:{air.port}" and silver.watch.status()["peer_refused"] >= 1


def test_an_announce_is_honored_only_with_the_token_for_this_door(lan: _Lan, tmp_path: Path) -> None:
    _air, silver = _paired(lan, tmp_path)
    old = silver.peer_address
    door = f"{SILVER}:{silver.port}"

    def announce(token: str, dialed: str, local: str | None = door, door_port: int = 4777) -> dict[str, Any]:
        nonce = silver.nearby.door_get(bn.CHALLENGE)["nonce"]
        body = {"nonce": nonce, "device_id": "c" * 32, "label": "Air", "door_port": 4777,
                "proof": bn.reconnect_proof(token, b"asker", nonce, silver.pairing.device_id, dialed, "c" * 32, door_port)}
        return silver.nearby.door_post(bn.RECONNECT, body, IMPOSTOR, local)

    for label, call in (
        ("another token", lambda: announce("not the token", door)),
        ("a proof for another door", lambda: announce(TOKEN, f"{IMPOSTOR}:4666")),
        ("a proof for another door port of the asker", lambda: announce(TOKEN, door, door_port=4778)),
    ):
        with pytest.raises(PairingError) as refused:
            call()
        assert refused.value.code == "not_remembered" and silver.peer_address == old, label
    with pytest.raises(PairingError) as off_lan:
        announce(TOKEN, door, local=None)
    assert off_lan.value.code == "lan_only" and silver.peer_address == old, "a door that cannot name its own address proves nothing"
    assert announce(TOKEN, door)["label"] == "Silver", "control: the same call with the token and the dialed door IS honored"
    assert silver.peer_address == f"{IMPOSTOR}:4777"


# ---------------------------------------------------------------- the real door names its own address


def test_the_lan_door_binds_a_proof_to_the_address_it_was_dialed_on(tmp_path: Path) -> None:
    """Real sockets: the door hands the reconnect the local address of the accepted connection."""
    pairing = PairingService(tmp_path, engine_port=lambda: 1, label="Silver", set_peer=lambda _c: None, hardware_ports=dict)
    nearby = bn.NearbyService(pairing, tmp_path, dict)
    pairing.nearby = nearby
    pairing.adopt(PairingRecord(peer_device_id="old", peer_label="Air", peer_host="10.0.0.1", peer_door_port=4000,
                                token=TOKEN, door_port=0, paired_at="t", role="inviter"))
    door = Door(pairing, lambda: "http://127.0.0.1:1", 0)
    try:
        def dial(host: str, claimed: str) -> httpx.Response:
            base = f"http://{host}:{door.port}"
            with httpx.Client(timeout=3) as client:
                nonce = client.get(f"{base}{bn.CHALLENGE}").json()["nonce"]
                proof = bn.reconnect_proof(TOKEN, b"asker", nonce, pairing.device_id, f"{claimed}:{door.port}", "c" * 32, 4777)
                return client.post(f"{base}{bn.RECONNECT}", json={"nonce": nonce, "device_id": "c" * 32, "label": "Air",
                                                                 "door_port": 4777, "proof": proof})

        honest = dial("127.0.0.1", "127.0.0.1")
        assert honest.status_code == 200, honest.text
        assert pairing.record is not None and pairing.record.peer_door_port == 4777
        relayed = dial("127.0.0.1", "10.9.9.9")
        assert relayed.status_code == 403 and relayed.json()["detail"]["code"] == "not_remembered", relayed.text
        alias = socket.socket()
        try:
            alias.bind(("127.0.0.2", 0))
        except OSError:
            pytest.skip("UNAVAILABLE: no second loopback address (127.0.0.2) on this host, so the two-address half did not run. Not a pass.")
        finally:
            alias.close()
        assert dial("127.0.0.2", "127.0.0.2").status_code == 200, "the door answers on every address, and names the one dialed"
        assert dial("127.0.0.2", "127.0.0.1").status_code == 403, "if the door names a fixed address then a relay on its other address passes"
    finally:
        door.stop()
        pairing.shutdown()
