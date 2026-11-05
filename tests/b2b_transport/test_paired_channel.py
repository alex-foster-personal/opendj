"""B2BNET-14: the paired-channel contract every peer read relies on, path by path.

The two-engine test proves peer features ride the internet door end to end; this pins the
mapping in between, so a regression names its rule. ``InternetChannel.send`` is the one seam
(``InternetService.request`` in production); here it is a recorder that answers or refuses the way
the service does. The LAN side runs against a real loopback HTTP server, not a mock transport.

- [if] an internet answer does not come back as an httpx.Response with its status, body and content type [then] fail
- [if] a query string is not split off the door path, or a POST body is not carried [then] fail
- [if] "partner not on the relay" does not raise an httpx.TransportError the existing handlers name [then] fail
- [if] any OTHER pairing refusal is swallowed into "unreachable" (overshoot) [then] fail
- [if] an httpx.Timeout's read budget is not the door message's wait, or an unbounded one is accepted [then] fail
- [if] a LAN read drops the pairing's bearer token [then] fail
- [if] a peer read asks the paired channel [then] the LAN and internet doors answer it alike, [else stop]
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from apps.b2b_pairing.b2b_pairing import PairingError
from apps.b2b_transport.channel import INTERNET_LABEL, ChannelUnreachable, InternetChannel, LanChannel
from apps.b2b_transport.door import DoorAnswer, DoorRequest

pytestmark = [pytest.mark.requirement("B2BNET-14")]

TOKEN = "t0ken"


class _Door:
    """Records what the channel sent and answers like ``InternetService.request``."""

    def __init__(self, answer: DoorAnswer | None = None, refusal: PairingError | None = None) -> None:
        self.answer, self.refusal = answer, refusal
        self.sent: list[tuple[DoorRequest, float]] = []

    def __call__(self, request: DoorRequest, timeout: float) -> DoorAnswer:
        self.sent.append((request, timeout))
        if self.refusal is not None:
            raise self.refusal
        assert self.answer is not None
        return self.answer


def test_internet_answer_is_a_real_response_with_status_body_and_type() -> None:
    door = _Door(DoorAnswer(200, "application/json", b'{"title": "A"}'))
    response = InternetChannel(door, TOKEN).get("/api/v1/b2b/peer/now-playing?deck=1", 2.0)
    assert isinstance(response, httpx.Response)
    assert (response.status_code, response.json(), response.headers["content-type"]) == (200, {"title": "A"}, "application/json")
    request, timeout = door.sent[0]
    assert (request.method, request.path, request.query, timeout) == ("GET", "/api/v1/b2b/peer/now-playing", "deck=1", 2.0)


def test_internet_post_carries_its_body_and_a_non_200_status_stays_a_status() -> None:
    door = _Door(DoorAnswer(409, "application/json", b'{"detail": "busy"}'))
    response = InternetChannel(door, TOKEN).post("/api/v1/link/relay/watch", b'{"a": 1}', 3.0)
    assert response.status_code == 409 and response.is_error  # a refusal answer, not a transport failure
    assert (door.sent[0][0].method, door.sent[0][0].body) == ("POST", b'{"a": 1}')


@pytest.mark.parametrize("code", ["peer_unreachable", "not_paired"])
def test_partner_off_the_relay_is_a_transport_error(code: str) -> None:
    channel = InternetChannel(_Door(refusal=PairingError(code, "partner is away", 409)), TOKEN)
    with pytest.raises(httpx.TransportError) as caught:
        channel.get("/api/v1/b2b/cursor/self", 1.0)
    assert isinstance(caught.value, ChannelUnreachable) and code in str(caught.value)


def test_other_refusals_are_not_swallowed_into_unreachable() -> None:
    channel = InternetChannel(_Door(refusal=PairingError("relay_unconfigured", "no relay", 503)), TOKEN)
    with pytest.raises(PairingError) as caught:
        channel.get("/api/v1/b2b/cursor/self", 1.0)
    assert caught.value.code == "relay_unconfigured" and not isinstance(caught.value, httpx.TransportError)


def test_timeout_read_budget_is_the_wait_and_an_unbounded_one_is_refused() -> None:
    door = _Door(DoorAnswer(200, "application/json", b"{}"))
    channel = InternetChannel(door, TOKEN)
    channel.post("/x", b"{}", httpx.Timeout(4.5, connect=1.0))
    assert door.sent[0][1] == 4.5
    with pytest.raises(ValueError, match="finite read timeout"):
        channel.get("/x", httpx.Timeout(None))
    assert len(door.sent) == 1, "an unbounded wait reached the relay"


def test_internet_channel_names_itself_without_a_host_and_ignores_bind() -> None:
    channel = InternetChannel(_Door(), TOKEN)
    assert (channel.kind, channel.label, channel.token) == ("internet", INTERNET_LABEL, TOKEN)
    with httpx.Client() as client:
        assert channel.bind(client) is channel


# ---------------------------------------------------------------- the LAN door, over real HTTP


class _EchoHandler(BaseHTTPRequestHandler):
    def _answer(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        body = json.dumps({"path": self.path, "auth": self.headers.get("authorization"),
                           "body": self.rfile.read(length).decode()}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _answer

    def log_message(self, *_args: object) -> None:
        return


@pytest.fixture
def lan_url() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _EchoHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_lan_channel_sends_the_bearer_token_and_body(lan_url: str) -> None:
    with httpx.Client() as client:
        channel = LanChannel(lan_url, TOKEN, client)
        got = channel.get("/api/v1/b2b/peer/now-playing?deck=1", 2.0).json()
        assert got == {"path": "/api/v1/b2b/peer/now-playing?deck=1", "auth": f"Bearer {TOKEN}", "body": ""}
        posted = channel.post("/api/v1/link/relay/watch", b'{"a": 1}', 2.0).json()
        assert (posted["auth"], posted["body"]) == (f"Bearer {TOKEN}", '{"a": 1}')
        assert (channel.kind, channel.label) == ("lan", lan_url)


def test_lan_channel_without_a_token_sends_no_auth_header(lan_url: str) -> None:
    with httpx.Client() as client:
        assert LanChannel(lan_url, None, client).get("/x", 2.0).json()["auth"] is None


def test_lan_channel_bind_swaps_only_the_client(lan_url: str) -> None:
    with httpx.Client() as first, httpx.Client() as second:
        bound = LanChannel(lan_url, TOKEN, first).bind(second)
        assert isinstance(bound, LanChannel) and bound.client is second and (bound.url, bound.token) == (lan_url, TOKEN)
