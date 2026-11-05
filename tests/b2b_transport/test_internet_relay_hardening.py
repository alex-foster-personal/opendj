"""B2BNET-06 and B2BNET-08 client side, against the REAL local relay (workerd via ``wrangler dev --local``).

Around an internet invite or a big door answer, the inviter gives its nameplate back, refusals
are named, and the session survives:

[if] a relay or network fault keeps the nameplate, hides a refusal, or kills the session [then] fail, [else stop].

No mocks: the relay is the Worker, the pairing and Noise code is production code, and the one
fault (a proxy that refuses WebSocket upgrades) is a real TCP proxy in front of the real relay.

- [if] the inviter's socket fails to open after the nameplate POST and the slot stays taken [then] fail
- [if] a relay 429 reaches the caller as anything but a named 429 refusal [then] fail
- [if] a door answer bigger than one relay frame kills the session or arrives changed [then] fail
- [if] a door answer over the message cap is sent at all, or answers anything but 413 [then] fail
- [if] an outgoing door request over the cap reaches the wire [then] fail
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from apps.b2b_pairing.b2b_pairing import PairingError
from apps.b2b_transport.door import DoorAnswer, DoorRequest
from apps.b2b_transport.internet import (
    MAX_DOOR_MESSAGE_BYTES,
    DoorChannel,
    Hello,
    InternetInvite,
    InternetPairing,
    join_over_relay,
)
from tests.b2b_transport.relay_harness import WebSocketBlocker, local_relay, relay_or_skip

pytestmark = [
    pytest.mark.requirement("B2BNET-06"),
    pytest.mark.requirement("B2BNET-08"),
    relay_or_skip(),
]

DEVICE_A, DEVICE_B = "a" * 32, "b" * 32
#: Production cap from (private path) NAMEPLATES_OPEN_PER_CLIENT.
OPEN_PER_CLIENT = 10


@pytest.fixture
def fresh_relay(tmp_path: Path) -> Iterator[str]:
    """A relay of its own: these tests read which nameplate the directory hands out next."""
    with local_relay(tmp_path / "relay") as base:
        yield base


@pytest.fixture(scope="module")
def relay(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    with local_relay(tmp_path_factory.mktemp("relay")) as base:
        yield base


def _pair(base: str) -> tuple[InternetPairing, InternetPairing, InternetInvite]:
    done = threading.Event()
    got: list[InternetPairing] = []
    invite = InternetInvite(base, DEVICE_A, "A", lambda p: (got.append(p), done.set()))
    joiner = join_over_relay(base, invite.code)
    assert done.wait(10), "inviter never learned it was paired"
    return got[0], joiner, invite


class Sized:
    """A door handler answering ``size`` bytes of fresh random hex, recording what it sent."""

    def __init__(self, size: int) -> None:
        self.size = size
        self.sent: list[bytes] = []

    def __call__(self, request: DoorRequest) -> DoorAnswer:
        body = os.urandom(self.size // 2).hex().encode()
        self.sent.append(body)
        return DoorAnswer(200, "application/octet-stream", body)


def _channels(base: str, a: InternetPairing, b: InternetPairing, serve_a: Sized) -> tuple[DoorChannel, DoorChannel]:
    chan_a = DoorChannel(base, a, serve_a, Hello(DEVICE_A, "A"), lambda _h: None, record_wire=True).start()
    chan_b = DoorChannel(base, b, Sized(16), Hello(DEVICE_B, "B"), lambda _h: None, record_wire=True).start()
    assert chan_a.wait_connected(20) and chan_b.wait_connected(20), (chan_a.status(), chan_b.status())
    return chan_a, chan_b


# ---------------------------------------------------------------- the inviter's nameplate


def test_inviter_releases_its_nameplate_when_its_socket_cannot_open(fresh_relay: str) -> None:
    blocker = WebSocketBlocker(fresh_relay)
    try:
        with pytest.raises(PairingError) as refused:
            InternetInvite(blocker.url, DEVICE_A, "A", lambda _p: None)
        assert refused.value.code == "relay_http_502", refused.value.code
        assert blocker.blocked == 1, "the fault was never injected"
        allocated = json.loads(blocker.bodies[0])["nameplate"]
    finally:
        blocker.close()
    # The directory hands out the smallest free nameplate, so the next POST gets the same one
    # back only if the failed invite released it.
    again = httpx.post(f"{fresh_relay}/v1/nameplates", timeout=10)
    assert again.status_code == 201
    assert again.json()["nameplate"] == allocated, "the inviter leaked its nameplate when its socket failed"


def test_a_relay_429_is_a_named_refusal(fresh_relay: str) -> None:
    held = [InternetInvite(fresh_relay, DEVICE_A, "A", lambda _p: None) for _ in range(OPEN_PER_CLIENT)]
    try:
        with pytest.raises(PairingError) as refused:
            InternetInvite(fresh_relay, DEVICE_A, "A", lambda _p: None)
        assert (refused.value.code, refused.value.status) == ("too_many_open_invites", 429)
    finally:
        for invite in held:
            invite.cancel()


# ---------------------------------------------------------------- big door messages


def test_a_door_answer_bigger_than_a_relay_frame_arrives_whole_and_the_session_survives(relay: str) -> None:
    a, b, invite = _pair(relay)
    serve_a = Sized(600_000)  # about 10 relay frames once base64 and JSON wrap it
    chan_a, chan_b = _channels(relay, a, b, serve_a)
    try:
        sessions = chan_b.sessions_established
        answer = chan_b.request(DoorRequest("GET", "/api/v1/b2b/artwork"), timeout=20)
        assert answer.status == 200
        assert answer.body == serve_a.sent[0], "the big answer arrived changed"
        assert chan_a.wire_log is not None
        assert max(len(f) for f in chan_a.wire_log) <= 65536, "a frame over the relay's 64 KiB cap was sent"
        assert sum(1 for f in chan_a.wire_log if f[0] == 3) >= 10, "the answer was not split across frames"
        small = chan_b.request(DoorRequest("GET", "/api/v1/b2b/now-playing"), timeout=10)
        assert small.status == 200
        assert chan_b.sessions_established == sessions, "the big answer cost the session"
        assert chan_a.frames_rejected == 0 and chan_b.frames_rejected == 0
    finally:
        chan_a.stop()
        chan_b.stop()
        invite.cancel()


def test_a_door_answer_over_the_cap_is_refused_413_and_the_session_survives(relay: str) -> None:
    a, b, invite = _pair(relay)
    serve_a = Sized(MAX_DOOR_MESSAGE_BYTES)  # over the cap once base64 and JSON wrap it
    chan_a, chan_b = _channels(relay, a, b, serve_a)
    try:
        sessions = chan_b.sessions_established
        answer = chan_b.request(DoorRequest("GET", "/api/v1/b2b/artwork"), timeout=20)
        assert answer.status == 413, answer.status
        assert "door_message_too_large" in answer.body.decode()
        assert serve_a.sent, "the handler never ran, so the refusal proves nothing"
        serve_a.size = 16
        assert chan_b.request(DoorRequest("GET", "/api/v1/b2b/now-playing"), timeout=10).status == 200
        assert chan_b.sessions_established == sessions, "the refusal cost the session"
    finally:
        chan_a.stop()
        chan_b.stop()
        invite.cancel()


def test_an_outgoing_request_over_the_cap_never_reaches_the_wire(relay: str) -> None:
    a, b, invite = _pair(relay)
    chan_a, chan_b = _channels(relay, a, b, Sized(16))
    try:
        assert chan_b.wire_log is not None
        before = len(chan_b.wire_log)
        with pytest.raises(PairingError) as refused:
            chan_b.request(DoorRequest("POST", "/b2b-relay/state", body=b"x" * MAX_DOOR_MESSAGE_BYTES), timeout=10)
        assert (refused.value.code, refused.value.status) == ("door_message_too_large", 413)
        assert len(chan_b.wire_log) == before, "an over-cap request was put on the wire"
        assert chan_b.request(DoorRequest("GET", "/api/v1/b2b/now-playing"), timeout=10).status == 200
    finally:
        chan_a.stop()
        chan_b.stop()
        invite.cancel()
