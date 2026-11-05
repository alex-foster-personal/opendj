"""B2BNET-04: a door message means the same thing on the LAN and over the internet.

[if] a door request takes the internet path [then] it gets the LAN door's status and body, the same 401 for a wrong token, and 404 not_over_internet for the pairing legs, [else stop].

A REAL LAN ``Door`` (the unchanged ``apps.b2b_pairing.b2b_door`` server on loopback) answers
through the LAN adapter's HTTP client; the same request answered by ``serve_door_request``
(what the internet channel calls) must give the same status and body.

- [if] the LAN and internet paths answer one door request differently [then] fail
- [if] the internet path serves a paired read without the token check the LAN door applies [then] fail
- [if] the pairing legs are reachable through the internet door [then] fail
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from apps.b2b_pairing.b2b_door import FINISH, HELLO, START
from apps.b2b_pairing.b2b_pairing import PairingError
from apps.b2b_pairing.b2b_service import PairingService
from apps.b2b_transport.door import DoorRequest, serve_door_request
from apps.b2b_transport.internet_service import _InternetHost
from apps.b2b_transport.lan import Door, lan_door_request

pytestmark = pytest.mark.requirement("B2BNET-04")

TOKEN = "parity-token"


class Host:
    """The smallest ``PairingHost``: paired with TOKEN, no invite, no nearby, no relay."""

    device_id = "d" * 32
    label = "Parity Laptop"
    nearby = None
    relay = None

    def paired_token(self) -> str | None:
        return TOKEN

    def invite_open(self) -> bool:
        return False

    def pair_start(self, body: dict[str, Any], source_ip: str) -> dict[str, Any]:
        raise PairingError("invite_closed", "no invite", 410)

    def pair_finish(self, body: dict[str, Any]) -> dict[str, Any]:
        raise PairingError("invite_closed", "no invite", 410)


@pytest.fixture
def door() -> Iterator[Door]:
    lan_door = Door(Host(), lambda: "http://127.0.0.1:9", port=0, bind="127.0.0.1")
    yield lan_door
    lan_door.stop()


@pytest.mark.parametrize("request_", [
    DoorRequest("GET", HELLO),
    DoorRequest("GET", "/not/a/door/path"),
    DoorRequest("POST", START, body=b'{"msg_a": "x"}'),
    DoorRequest("POST", "/b2b-relay/ping", body=b"{}"),
    DoorRequest("POST", "/b2b-nearby/ask", body=b"{}"),
], ids=["hello", "unknown", "pair-start", "relay-off", "nearby-off"])
def test_lan_and_internet_answer_alike(door: Door, request_: DoorRequest) -> None:
    lan = lan_door_request(f"http://127.0.0.1:{door.port}", TOKEN, request_, timeout=5)
    internet = serve_door_request(Host(), "http://127.0.0.1:9", TOKEN, request_)
    assert (internet.status, internet.json()) == (lan.status, lan.json()), request_.path


def test_paired_reads_still_check_the_token(door: Door) -> None:
    request_ = DoorRequest("GET", "/api/v1/b2b/now-playing")
    assert lan_door_request(f"http://127.0.0.1:{door.port}", "wrong-token", request_, timeout=5).status == 401
    assert serve_door_request(Host(), "http://127.0.0.1:9", "wrong-token", request_).status == 401
    # With the right token both reach the (here unreachable) engine and say so the same way.
    lan = lan_door_request(f"http://127.0.0.1:{door.port}", TOKEN, request_, timeout=5)
    internet = serve_door_request(Host(), "http://127.0.0.1:9", TOKEN, request_)
    assert lan.status == internet.status == 502


def test_the_internet_door_never_serves_the_pairing_legs(tmp_path: Path) -> None:
    lan = PairingService(tmp_path, lambda: 9, "Parity Laptop", lambda _config: None, dict)
    host = _InternetHost(lan, TOKEN)
    for path in (START, FINISH):
        answer = serve_door_request(host, "http://127.0.0.1:9", TOKEN, DoorRequest("POST", path, body=b"{}"))
        assert answer.status == 404 and answer.json()["detail"]["code"] == "not_over_internet", path
