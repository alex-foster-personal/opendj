"""B2BNET-01..04 security, against the REAL local relay (workerd via ``wrangler dev --local``).

[if] two laptops pair and talk through the real relay [then] a wrong code costs the inviter one attempt, five burn the invite, duplicated or replayed frames and sessions are never served, and no secret crosses the wire in clear, [else stop].

No mocks: the SPAKE2, Noise and AES-GCM code is the production code, the relay is the Worker,
and every byte crosses a loopback TCP tap so the tests can read exactly what the relay saw.
The door handler here is a plain echo so transport behavior is isolated from the engine
(the two-engine test covers the engine end).

- [if] a wrong code pairs, or does not spend one attempt on the inviter AND the relay [then] fail
- [if] a 6th code reaches the inviter after 5 wrong ones, or the nameplate stays open [then] fail
- [if] a frame the relay duplicates is served twice, or a recorded session replayed later is served at all [then] fail
- [if] the token, the 6-character code, a door body, the labels or the device ids appear on the wire [then] fail
- [if] the tap cannot see a byte we deliberately sent in clear (the positive control) [then] fail
- [if] a replayed handshake message 1 knocks out the live session [then] fail
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator

import pytest
from websockets.sync.client import connect

from apps.b2b_pairing.b2b_pairing import ALPHABET, MAX_ATTEMPTS, PairingError
from apps.b2b_transport.door import DoorAnswer, DoorRequest
from apps.b2b_transport.internet import (
    DoorChannel,
    Hello,
    InternetInvite,
    InternetPairing,
    join_over_relay,
    parse_internet_code,
)
from tests.b2b_transport.relay_harness import WireTap, local_relay, relay_or_skip

pytestmark = [
    pytest.mark.requirement("B2BNET-01"),
    pytest.mark.requirement("B2BNET-02"),
    pytest.mark.requirement("B2BNET-03"),
    pytest.mark.requirement("B2BNET-04"),
    relay_or_skip(),
]

LABEL_A, LABEL_B = "Booth Alpha Laptop", "Booth Bravo Laptop"
DEVICE_A, DEVICE_B = "a" * 32, "b" * 32
SECRET_BODY = b"door-body-plaintext-7f3a9c"


@pytest.fixture(scope="module")
def relay(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    with local_relay(tmp_path_factory.mktemp("relay")) as base:
        yield base


@pytest.fixture
def tap(relay: str) -> Iterator[WireTap]:
    wire = WireTap(relay)
    yield wire
    wire.close()


def _wrong_code(code: str) -> str:
    nameplate, six = parse_internet_code(code)
    swapped = ("0" if six[0] != "0" else "1") + six[1:]
    assert swapped != six and all(ch in ALPHABET for ch in swapped)
    return f"{nameplate}-{swapped[:3]}-{swapped[3:]}"


class Paired:
    def __init__(self) -> None:
        self.inviter: InternetPairing | None = None
        self.event = threading.Event()

    def __call__(self, pairing: InternetPairing) -> None:
        self.inviter = pairing
        self.event.set()


def _pair(base: str) -> tuple[InternetPairing, InternetPairing, InternetInvite]:
    paired = Paired()
    invite = InternetInvite(base, DEVICE_A, LABEL_A, paired)
    joiner = join_over_relay(base, invite.code)
    assert paired.event.wait(10), "inviter never learned it was paired"
    assert paired.inviter is not None
    return paired.inviter, joiner, invite


class Echo:
    """The door handler: echoes the request, counts how often it ran."""

    def __init__(self) -> None:
        self.served: list[DoorRequest] = []

    def __call__(self, request: DoorRequest) -> DoorAnswer:
        self.served.append(request)
        body = json.dumps({"path": request.path, "body": request.body.decode()}).encode()
        return DoorAnswer(200, "application/json", body)


def _channels(base: str, a: InternetPairing, b: InternetPairing, record: bool = False) -> tuple[DoorChannel, DoorChannel, Echo, Echo]:
    echo_a, echo_b = Echo(), Echo()
    chan_a = DoorChannel(base, a, echo_a, Hello(DEVICE_A, LABEL_A), lambda _h: None, record_wire=record).start()
    chan_b = DoorChannel(base, b, echo_b, Hello(DEVICE_B, LABEL_B), lambda _h: None, record_wire=record).start()
    assert chan_a.wait_connected(20) and chan_b.wait_connected(20), (chan_a.status(), chan_b.status())
    return chan_a, chan_b, echo_a, echo_b


def _until(check, what: str, budget: float = 10.0) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + budget
    while not check():
        assert time.monotonic() < deadline, f"timed out: {what}"
        time.sleep(0.05)


# ---------------------------------------------------------------- pairing


def test_wrong_code_is_refused_and_spends_one_attempt(relay: str) -> None:
    paired = Paired()
    invite = InternetInvite(relay, DEVICE_A, LABEL_A, paired)
    try:
        with pytest.raises(PairingError) as refused:
            join_over_relay(relay, _wrong_code(invite.code))
        assert refused.value.code == "wrong_code"
        assert invite.attempts_left == MAX_ATTEMPTS - 1
        assert not paired.event.is_set()
        # The right code still pairs afterwards: one wrong try does not burn the invite.
        joiner = join_over_relay(relay, invite.code)
        assert paired.event.wait(10) and paired.inviter is not None
        assert joiner.token == paired.inviter.token and joiner.room == paired.inviter.room
        assert len(joiner.room) == 43
    finally:
        invite.cancel()


def test_five_wrong_codes_close_the_nameplate_and_the_sixth_never_reaches_the_inviter(relay: str) -> None:
    paired = Paired()
    invite = InternetInvite(relay, DEVICE_A, LABEL_A, paired)
    try:
        for attempt in range(MAX_ATTEMPTS):
            with pytest.raises(PairingError) as refused:
                _join_retrying_busy(relay, _wrong_code(invite.code))
            assert refused.value.code == "wrong_code", f"attempt {attempt + 1}: {refused.value.code}"
        assert invite.attempts_left == 0
        _until(lambda: invite.state == "burned", f"inviter state burned (is {invite.state})")
        with pytest.raises(PairingError) as closed:
            _join_retrying_busy(relay, invite.code)  # even the RIGHT code: the invite is gone
        assert closed.value.code == "invite_closed" and closed.value.status == 410
        assert not paired.event.is_set()
    finally:
        invite.cancel()


def _join_retrying_busy(base: str, code: str) -> InternetPairing:
    """The previous joiner's close reaches the relay asynchronously; retry only on ``busy``."""
    for _ in range(100):
        try:
            return join_over_relay(base, code)
        except PairingError as exc:
            if exc.code != "busy":
                raise
            time.sleep(0.05)
    raise AssertionError("relay joiner slot never freed")


def test_code_format_and_missing_inviter_are_named(relay: str) -> None:
    with pytest.raises(PairingError) as bad:
        join_over_relay(relay, "K7Q-4MX")  # a LAN code has no nameplate
    assert bad.value.code == "bad_code_format"
    with pytest.raises(PairingError) as closed:
        join_over_relay(relay, "9876-K7Q-4MX")
    assert closed.value.code == "invite_closed"


# ---------------------------------------------------------------- the sealed channel


def test_door_messages_cross_both_ways(relay: str) -> None:
    a, b, invite = _pair(relay)
    chan_a, chan_b, echo_a, echo_b = _channels(relay, a, b)
    try:
        answer = chan_b.request(DoorRequest("POST", "/b2b-relay/ping", body=b"from b"), timeout=10)
        assert answer.status == 200 and answer.json() == {"path": "/b2b-relay/ping", "body": "from b"}
        answer = chan_a.request(DoorRequest("GET", "/api/v1/b2b/now-playing"), timeout=10)
        assert answer.json()["path"] == "/api/v1/b2b/now-playing"
        assert [r.path for r in echo_a.served] == ["/b2b-relay/ping"]
        assert [r.path for r in echo_b.served] == ["/api/v1/b2b/now-playing"]
        _until(lambda: chan_a.peer == Hello(DEVICE_B, LABEL_B) and chan_b.peer == Hello(DEVICE_A, LABEL_A), "hellos")
    finally:
        chan_a.stop()
        chan_b.stop()
        invite.cancel()


def test_a_duplicated_frame_is_served_once(relay: str) -> None:
    a, b, invite = _pair(relay)
    chan_a, chan_b, _echo_a, echo_b = _channels(relay, a, b, record=True)
    try:
        chan_a.request(DoorRequest("POST", "/b2b-relay/state", body=b"once"), timeout=10)
        assert len(echo_b.served) == 1
        assert chan_a.wire_log is not None
        request_frame = next(f for f in reversed(chan_a.wire_log) if f[0] == 3)
        rejected_before = chan_b.frames_rejected
        chan_a._put(request_frame)  # exactly what a relay that duplicates a frame would deliver
        _until(lambda: chan_b.frames_rejected == rejected_before + 1, "replayed frame rejected")
        time.sleep(0.3)
        assert len(echo_b.served) == 1, "a replayed request was served twice"
        assert chan_b.connected, "a replayed frame must not tear down the live session"
    finally:
        chan_a.stop()
        chan_b.stop()
        invite.cancel()


def test_a_recorded_session_replayed_later_is_never_served(relay: str) -> None:
    a, b, invite = _pair(relay)
    chan_a, chan_b, echo_a, _echo_b = _channels(relay, a, b, record=True)
    try:
        chan_b.request(DoorRequest("POST", "/b2b-relay/state", body=b"original"), timeout=10)
        served_before = len(echo_a.served)
        recorded = list(chan_b.wire_log or [])  # the joiner's side: handshake 1, hello, the request
        assert any(f[0] == 1 for f in recorded) and any(f[0] == 3 for f in recorded), "nothing recorded"
        chan_b.stop()
        # The attacker (a relay, or anyone holding the room id) takes B's place and replays it all.
        rejected_before = chan_a.frames_rejected
        with connect(f"{relay.replace('http', 'ws')}/v1/rooms/{a.room}/ws", compression=None) as attacker:
            for frame in recorded:
                attacker.send(frame)
            _until(lambda: chan_a.frames_rejected > rejected_before, "replayed data rejected")
            time.sleep(0.5)
        assert len(echo_a.served) == served_before, "a replayed old session was served"
        assert not chan_a.connected, "a replayed handshake must not establish a session"
    finally:
        chan_a.stop()
        invite.cancel()


def test_a_replayed_handshake_does_not_knock_out_the_live_session(relay: str) -> None:
    a, b, invite = _pair(relay)
    chan_a, chan_b, _echo_a, _echo_b = _channels(relay, a, b, record=True)
    try:
        assert chan_b.wire_log is not None
        hs1 = next(f for f in chan_b.wire_log if f[0] == 1)  # the joiner (b) speaks first
        chan_b._put(hs1)
        time.sleep(0.5)
        answer = chan_b.request(DoorRequest("POST", "/b2b-relay/ping", body=b"still"), timeout=10)
        assert answer.json()["body"] == "still"
        assert chan_a.connected and chan_b.connected
    finally:
        chan_a.stop()
        chan_b.stop()
        invite.cancel()


# ---------------------------------------------------------------- what the relay can read


def test_the_relay_sees_only_ciphertext(tap: WireTap) -> None:
    a, b, invite = _pair(tap.url)
    code6 = parse_internet_code(invite.code)[1]
    chan_a, chan_b, _echo_a, _echo_b = _channels(tap.url, a, b)
    try:
        answer = chan_b.request(DoorRequest("POST", "/b2b-relay/ping", body=SECRET_BODY), timeout=10)
        assert answer.json()["body"] == SECRET_BODY.decode(), "the door message did not round-trip"
        # Positive control: a marker sent in clear through the same tap must be found.
        with connect(f"{tap.url.replace('http', 'ws')}/v1/rooms/{a.room}/ws", compression=None) as clear:
            clear.send("CLEAR-CONTROL-MARKER")
        time.sleep(0.3)
    finally:
        chan_a.stop()
        chan_b.stop()
        invite.cancel()
    readable = tap.capture.readable()
    frames = tap.capture.frames()
    assert b"CLEAR-CONTROL-MARKER" in readable, "the tap cannot read a clear frame, so its silence proves nothing"
    assert a.room.encode() in readable, "the room id rides the URL by design; the tap must see it"
    assert sum(1 for d, op, _ in frames if d == "up" and op == 2) >= 4, "too few binary frames captured"
    for secret, name in [(a.token.encode(), "token"), (code6.encode(), "6-character code"), (SECRET_BODY, "door body"),
                         (LABEL_A.encode(), "inviter label"), (LABEL_B.encode(), "joiner label"),
                         (DEVICE_A.encode(), "inviter device id"), (DEVICE_B.encode(), "joiner device id")]:
        assert secret not in readable, f"the relay could read the {name}"
        assert secret not in tap.capture.all_bytes(), f"the {name} crossed the wire in clear"
