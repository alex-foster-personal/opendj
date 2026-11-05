"""B2BBOOTH-97: the sealed LAN datagram lane: sealing, replay protection, newest wins, the probe and the
fallback to the door CTL lane.

- [if] a tampered, replayed, wrong-epoch, unknown-sender or reflected datagram is accepted, or a drop
  is not counted under its reason [then] fail
- [if] a valid datagram (in order, or reordered within the window) is dropped [then] fail (overshoot)
- [if] an older datagram moves a control or a playhead after a newer one landed [then] fail
- [if] the CTL traffic rides UDP before a pong proved both directions, or stays on UDP after pongs
  stop [then] fail
- [if] with UDP blocked the booth does not report the door lane active, or a control does not arrive [then] fail
- [if] the datagram lane breaks any line above [then] fail, [else stop].
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest

from apps.b2b_booth.booth import Booth
from apps.b2b_booth.dgram import DGRAM_STALE_S, PING_S, PROBE_PING_S, DatagramLink, NewestFilter, Repeats, message_key
from apps.b2b_booth.protocol import BoothError, validate_message
from apps.b2b_transport.datagram import (
    HEADER,
    MAX_PAYLOAD,
    DatagramError,
    DatagramSocket,
    SealedDatagrams,
    derive_key,
)

pytestmark = [pytest.mark.requirement("B2BBOOTH-97")]

TOKEN = "booth-datagram-token-0123456789"
E1, E2 = "0123456789abcdef", "fedcba9876543210"


def _pair(epoch: str = E1) -> tuple[SealedDatagrams, SealedDatagrams]:
    inviter, joiner = SealedDatagrams(TOKEN, "inviter"), SealedDatagrams(TOKEN, "joiner")
    sid_i, sid_j = inviter.begin(epoch), joiner.begin(epoch)
    assert inviter.set_peer(epoch, sid_j) and joiner.set_peer(epoch, sid_i)
    return inviter, joiner


def _flip(frame: bytes, index: int) -> bytes:
    return frame[:index] + bytes([frame[index] ^ 0x01]) + frame[index + 1:]


# ---------------------------------------------------------------- sealing (positive and negative controls)


def test_a_sealed_datagram_opens_on_the_partner_with_its_counter() -> None:
    inviter, joiner = _pair()
    frames = [inviter.seal(f"m{i}".encode()) for i in range(3)]
    assert [joiner.open(f) for f in frames] == [(0, b"m0"), (1, b"m1"), (2, b"m2")]
    assert joiner.received == 3 and sum(joiner.dropped.values()) == 0
    assert b"m0" not in frames[0], "the payload travels encrypted"


@pytest.mark.parametrize(("where", "reason"), [
    ("ciphertext", "tampered"), ("tag", "tampered"), ("counter", "tampered"),
    ("epoch", "wrong_epoch"), ("sid", "unknown_sender"), ("magic", "malformed"),
])
def test_a_tampered_datagram_is_dropped_and_counted_by_reason(where: str, reason: str) -> None:
    inviter, joiner = _pair()
    frame = inviter.seal(b'{"kind":"ctl"}')
    index = {"magic": 0, "epoch": 4, "sid": 12, "counter": 27, "ciphertext": HEADER.size, "tag": len(frame) - 1}[where]
    assert joiner.open(_flip(frame, index)) is None, f"if a datagram with a flipped {where} bit opens then broken"
    assert joiner.dropped[reason] == 1 and joiner.received == 0
    assert joiner.open(frame) == (0, b'{"kind":"ctl"}'), "positive control: the untampered frame still opens"


def test_a_replayed_datagram_is_dropped_and_counted_but_a_reordered_one_opens() -> None:
    inviter, joiner = _pair()
    first, second = inviter.seal(b"a"), inviter.seal(b"b")
    assert joiner.open(second) == (1, b"b")
    assert joiner.open(first) == (0, b"a"), "overshoot control: a reordered, never-seen datagram is not a replay"
    assert joiner.open(second) is None and joiner.open(first) is None
    assert joiner.dropped["replayed"] == 2


def test_a_datagram_of_another_epoch_is_dropped_and_counted() -> None:
    old_inviter, _ = _pair(E2)
    _, joiner = _pair(E1)
    assert joiner.open(old_inviter.seal(b"x")) is None
    assert joiner.dropped["wrong_epoch"] == 1


def test_an_old_sender_session_does_not_open_after_the_partner_reannounced() -> None:
    inviter, joiner = _pair()
    stale = inviter.seal(b"before restart")
    joiner.set_peer(E1, inviter.begin(E1))  # the inviter's engine restarted inside the epoch
    assert joiner.open(stale) is None and joiner.dropped["unknown_sender"] == 1
    assert joiner.open(inviter.seal(b"after")) == (0, b"after"), "the new session's counter starts again from 0"


def test_a_reflected_datagram_does_not_open_on_its_sender() -> None:
    inviter, _ = _pair()
    own = inviter.seal(b"mine")
    inviter.set_peer(E1, inviter.sid or "")  # even if an attacker could make its own sid look like the partner's
    assert inviter.open(own) is None and inviter.dropped["tampered"] == 1, "the direction is in the key"


def test_keys_differ_by_direction_epoch_sid_and_token() -> None:
    base = derive_key(TOKEN, E1, "00" * 8, "inviter")
    assert len(base) == 32
    assert base != derive_key(TOKEN, E1, "00" * 8, "joiner")
    assert base != derive_key(TOKEN, E2, "00" * 8, "inviter")
    assert base != derive_key(TOKEN, E1, "01" * 8, "inviter")
    assert base != derive_key(TOKEN + "x", E1, "00" * 8, "inviter")
    assert base == derive_key(TOKEN, E1, "00" * 8, "inviter")


def test_no_session_no_token_and_oversize_are_refused_loudly() -> None:
    with pytest.raises(DatagramError):
        SealedDatagrams("", "inviter")
    lone = SealedDatagrams(TOKEN, "joiner")
    assert lone.open(b"OBD1" + bytes(60)) is None
    assert lone.dropped["no_session"] == 1
    inviter, _ = _pair()
    with pytest.raises(DatagramError):
        inviter.seal(b"x" * (MAX_PAYLOAD + 1))
    assert len(inviter.seal(b"x" * MAX_PAYLOAD)) <= 1200


def test_two_real_sockets_exchange_sealed_datagrams_on_loopback() -> None:
    got: list[tuple[int, bytes]] = []
    a = DatagramSocket(TOKEN, "inviter", "127.0.0.1", bind_host="127.0.0.1")
    b = DatagramSocket(TOKEN, "joiner", "127.0.0.1", bind_host="127.0.0.1")
    try:
        a.start(lambda c, p, addr: None)
        b.start(lambda c, p, addr: got.append((c, p)))
        sid_a, sid_b = a.begin(E1, "inviter"), b.begin(E1, "joiner")
        assert not a.send(b"too early"), "no partner session yet: nothing is sent"
        a.set_peer(E1, b.port, sid_b)
        b.set_peer(E1, a.port, sid_a)
        assert a.ready and a.send(b"hello")
        deadline = time.monotonic() + 2
        while not got and time.monotonic() < deadline:
            time.sleep(0.01)
        assert got == [(0, b"hello")]
        assert b.status()["received"] == 1 and a.status()["sent"] == 1
    finally:
        a.close()
        b.close()


# ---------------------------------------------------------------- newest wins, repeats, the probe


def test_newest_filter_drops_an_older_datagram_per_key_only() -> None:
    f = NewestFilter()
    assert f.admit("ctl:strip:1|fader", 5)
    assert not f.admit("ctl:strip:1|fader", 4) and not f.admit("ctl:strip:1|fader", 5)
    assert f.admit("ctl:strip:2|fader", 3), "another key is not held back by an unrelated newer datagram"
    assert f.admit("ctl:strip:1|fader", 6)
    assert f.stale == 2


def test_message_keys() -> None:
    assert message_key({"kind": "ctl", "unit": "strip:1", "ctl": "fader"}) == "ctl:strip:1|fader"
    assert message_key({"kind": "pos", "deck": 2}) == "pos:2"
    assert message_key({"kind": "renew", "side": "joiner"}) == "renew:joiner"
    with pytest.raises(ValueError):
        message_key({"kind": "ping"})


def test_repeats_send_a_controls_newest_value_twice_unless_replaced() -> None:
    r = Repeats()
    r.schedule("ctl:strip:1|fader", {"value": 0.1}, 10.0)
    r.schedule("pos:1", {"deck": 1}, 10.0)
    assert r.take(10.01) == []
    assert r.take(10.05) == [("ctl:strip:1|fader", {"value": 0.1})]
    r.schedule("ctl:strip:1|fader", {"value": 0.2}, 10.06)  # a newer value replaces the pending repeat
    assert r.take(10.15) == [("ctl:strip:1|fader", {"value": 0.2})]
    assert r.take(10.3) == [("ctl:strip:1|fader", {"value": 0.2})]
    assert r.take(11.0) == [] and r.sent == 3


def test_the_link_stays_on_the_door_until_a_pong_proves_both_directions() -> None:
    link = DatagramLink()
    link.reset(True)
    assert link.update(0.0, peer_known=False) is None and link.active == "door"
    assert "partner's datagram socket" in (link.reason or "")
    assert link.ping_due(0.0, peer_known=True) and not link.ping_due(PROBE_PING_S / 2, peer_known=True)
    assert link.ping_due(PROBE_PING_S, peer_known=True), "the probe pings fast until the first pong"
    assert link.update(0.5, peer_known=True) is None and link.reason == "probing: no pong yet"
    link.on_pong(0.6, 12_000)
    assert link.update(0.6, peer_known=True) == "datagram" and link.reason is None
    assert link.ping_due(0.6, peer_known=True)
    assert not link.ping_due(0.6 + PROBE_PING_S, peer_known=True), "proven: steady pings, not probe pings"
    assert link.ping_due(0.6 + PING_S, peer_known=True)
    assert link.update(0.6 + DGRAM_STALE_S, peer_known=True) is None, "exactly the stale limit still holds"
    assert link.update(0.6 + DGRAM_STALE_S + 0.01, peer_known=True) == "door"
    assert "no pong" in (link.reason or "") and link.switches == 2
    link.on_pong(3.0, 12_000)
    assert link.update(3.0, peer_known=True) == "datagram", "pongs again: back on the datagram lane"


def test_without_a_channel_the_link_never_pings_or_leaves_the_door() -> None:
    link = DatagramLink()
    link.reset(False, "no datagram channel")
    assert not link.ping_due(0.0, peer_known=True)
    link.on_pong(0.0, 1)
    assert link.update(0.0, peer_known=True) is None and link.active == "door"


# ---------------------------------------------------------------- two engine booths, real UDP on loopback


def _clock() -> int:
    return time.monotonic_ns() // 1000


def _door(target: Booth) -> Any:
    def post(lane: str, envelope: dict[str, Any], timeout: float) -> dict[str, Any]:
        status, body = target.door_post(lane, json.dumps(envelope).encode())
        if status != 200:
            raise ConnectionError(f"door answered {status}: {body}")
        return body

    return post


class Blockable(DatagramSocket):
    """A real socket whose outgoing datagrams can be dropped (the iptables drop, in process)."""

    blocked = False

    def send(self, payload: bytes, addr: tuple[str, int] | None = None) -> bool:
        return False if self.blocked else super().send(payload, addr)


def _until(predicate: Any, timeout: float = 5.0, what: str = "") -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.01)


@pytest.fixture
def booths() -> Iterator[tuple[Booth, Booth, Blockable, Blockable]]:
    inviter, joiner = Booth(_clock), Booth(_clock)
    inviter.attach_transport(_door(joiner), "test")
    joiner.attach_transport(_door(inviter), "test")
    chan_i = Blockable(TOKEN, "inviter", "127.0.0.1", bind_host="127.0.0.1")
    chan_j = Blockable(TOKEN, "joiner", "127.0.0.1", bind_host="127.0.0.1")
    inviter.attach_datagram(chan_i)
    joiner.attach_datagram(chan_j)
    try:
        yield inviter, joiner, chan_i, chan_j
    finally:
        inviter.shutdown()
        joiner.shutdown()


def _lanes(booth: Booth) -> dict[str, Any]:
    return booth.view(enabled=True)["lanes"]


def _open(inviter: Booth, joiner: Booth) -> None:
    inviter.open("inviter")
    joiner.open("joiner")
    _until(lambda: inviter.view(enabled=True)["state"] == joiner.view(enabled=True)["state"] == "live", what="both live")


def _partner_values(booth: Booth, unit: str) -> list[float]:
    events: list[float] = []
    lock = threading.Lock()

    def on(event: dict[str, Any]) -> None:
        if event["type"] == "control" and event["control"]["unit"] == unit:
            with lock:
                events.append(event["control"]["value"])

    booth.subscribe(on)
    return events


def test_controls_ride_the_datagram_lane_once_the_probe_proves_it(booths: tuple[Booth, Booth, Blockable, Blockable]) -> None:
    inviter, joiner, _, _ = booths
    _open(inviter, joiner)
    _until(lambda: _lanes(inviter)["ctl_active"] == _lanes(joiner)["ctl_active"] == "datagram", what="datagram lane on both")
    seen = _partner_values(inviter, "strip:3")
    door_posts = _lanes(joiner)["ctl"]["posts"]
    for i in range(1, 21):
        joiner.intent({"kind": "control", "unit": "strip:3", "ctl": "fader", "value": i / 20})
    _until(lambda: seen and seen[-1] == 1.0, what="the joiner's last fader value at the inviter")
    assert seen == sorted(seen), "newest wins: the value never moves backwards"
    lanes = _lanes(joiner)
    assert lanes["datagram"]["channel"]["sent"] >= 20 and lanes["ctl"]["posts"] - door_posts <= 2, (
        "if the controls rode the door POSTs instead of datagrams then broken")
    assert _lanes(inviter)["datagram"]["channel"]["dropped"]["tampered"] == 0
    assert joiner.view(enabled=True)["peer_clock"]["rtt_us"] is not None


def test_udp_blocked_both_ways_keeps_the_door_lane_and_controls_still_arrive(
        booths: tuple[Booth, Booth, Blockable, Blockable]) -> None:
    inviter, joiner, chan_i, chan_j = booths
    chan_i.blocked = chan_j.blocked = True
    _open(inviter, joiner)
    time.sleep(DGRAM_STALE_S)
    seen = _partner_values(inviter, "strip:3")
    joiner.intent({"kind": "control", "unit": "strip:3", "ctl": "fader", "value": 0.7})
    _until(lambda: seen == [0.7], what="the control over the door")
    for b in (inviter, joiner):
        dgram = _lanes(b)["datagram"]
        assert _lanes(b)["ctl_active"] == "door" and dgram["pongs"] == 0 and dgram["pings"] > 0
        assert "no pong yet" in dgram["reason"], dgram


def test_udp_blocked_one_way_also_keeps_the_door_lane(booths: tuple[Booth, Booth, Blockable, Blockable]) -> None:
    inviter, joiner, _, chan_j = booths
    chan_j.blocked = True  # the joiner's datagrams (pings and pongs) never leave
    _open(inviter, joiner)
    time.sleep(DGRAM_STALE_S)
    assert _lanes(inviter)["ctl_active"] == "door" and _lanes(joiner)["ctl_active"] == "door"


def test_udp_blocked_mid_session_falls_back_and_the_newest_control_still_lands(
        booths: tuple[Booth, Booth, Blockable, Blockable]) -> None:
    inviter, joiner, _, chan_j = booths
    _open(inviter, joiner)
    _until(lambda: _lanes(joiner)["ctl_active"] == "datagram", what="datagram lane")
    seen = _partner_values(inviter, "strip:3")
    chan_j.blocked = True
    joiner.intent({"kind": "control", "unit": "strip:3", "ctl": "fader", "value": 0.42})  # lost on the wire
    _until(lambda: _lanes(joiner)["ctl_active"] == "door", timeout=DGRAM_STALE_S + 2, what="fallback to the door")
    _until(lambda: seen and seen[-1] == 0.42, what="the lost control re-sent over the door at the switch")
    joiner.intent({"kind": "control", "unit": "strip:3", "ctl": "fader", "value": 0.5})
    _until(lambda: seen[-1] == 0.5, what="the next control over the door")


# ---------------------------------------------------------------- wire shapes of the new kinds


@pytest.mark.parametrize(("message", "lane"), [
    ({"kind": "udp", "port": 40000, "sid": "0123456789abcdef"}, "ctl"),
    ({"kind": "ping", "t0_us": 5}, "dgram"),
    ({"kind": "pong", "t0_us": 5, "now_us": 9}, "dgram"),
    ({"kind": "ctl", "side": "joiner", "unit": "strip:3", "ctl": "fader", "value": 0.5, "cseq": 1, "at_link_us": 1}, "dgram"),
])
def test_the_new_kinds_validate_on_their_own_lane(message: dict[str, Any], lane: Any) -> None:
    assert validate_message(message, lane) == message


@pytest.mark.parametrize(("message", "lane", "code"), [
    ({"kind": "udp", "port": 40000, "sid": "0123456789abcdef"}, "dgram", "bad_kind"),  # never announced over UDP
    ({"kind": "ping", "t0_us": 5}, "ctl", "bad_kind"),
    ({"kind": "propose", "from": "joiner", "pid": 1, "op": {"op": "end"}}, "dgram", "bad_kind"),  # ops never ride UDP
    ({"kind": "udp", "port": 70000, "sid": "0123456789abcdef"}, "ctl", "bad_field"),
    ({"kind": "udp", "port": 40000, "sid": "XYZ"}, "ctl", "bad_field"),
    ({"kind": "pong", "t0_us": 5}, "dgram", "bad_field"),
])
def test_a_new_kind_off_its_lane_or_malformed_is_refused(message: dict[str, Any], lane: Any, code: str) -> None:
    with pytest.raises(BoothError) as err:
        validate_message(message, lane)
    assert err.value.code == code
