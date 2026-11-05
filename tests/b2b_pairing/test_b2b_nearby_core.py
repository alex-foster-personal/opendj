"""B2BNEAR-02..04 core: TXT, sealed answer, reconnect proofs, and the answerer's request guards.

No Bonjour and no second engine: the answerer half is a real ``NearbyService`` over a real
``PairingService`` in a temp state dir, fed the exact JSON its LAN door receives
(``door_post``), on an injected clock. tests/webui/test_b2b_nearby_two_engines.py runs the
same exchange between two real engines.

- [if] a request pairs this laptop before Accept [then] fail
- [if] Decline, or an expired request, can still be accepted [then] fail
- [if] a 4th request a minute from one laptop, or a 2nd while one waits, is held [then] fail
- [if] a reconnect proof made with another token, or a replayed challenge, is honored [then] fail
- [if] a tampered sealed answer opens [then] fail
- [if] the answerer gets asks, declines, floods and reconnects [then] only Accept or the paired token pairs, [else stop]
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from apps.b2b_pairing import b2b_nearby as bn
from apps.b2b_pairing.b2b_pairing import PairingError, PairingRecord
from apps.b2b_pairing.b2b_service import PairingService

pytestmark = [pytest.mark.requirement("B2BNEAR-02"), pytest.mark.requirement("B2BNEAR-03"),
              pytest.mark.requirement("B2BNEAR-04")]


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode()


def _service(tmp_path: Path, clock: _Clock) -> bn.NearbyService:
    pairing = PairingService(tmp_path, engine_port=lambda: 1, label="Laptop A", set_peer=lambda _c: None,
                             hardware_ports=dict)
    nearby = bn.NearbyService(pairing, tmp_path, dict, clock=clock)
    pairing.nearby = nearby
    return nearby


def _request_body(private: X25519PrivateKey | None = None) -> dict[str, Any]:
    key = private or X25519PrivateKey.generate()
    return {"v": "1", "device_id": "b" * 32, "label": "Laptop B", "door_port": 40001,
            "pub": _b64(key.public_key().public_bytes_raw())}


def test_txt_carries_name_accepting_and_tag_and_the_tag_is_only_the_partners() -> None:
    txt = bn.build_txt("a" * 32, "Peter's MacBook", True, bn.pairing_tag("tok"))
    assert " " not in "".join(txt.values()), "if a TXT value holds a space then dns-sd -L splits it"
    assert unquote(txt["name"]) == "Peter's MacBook" and txt["acc"] == "1" and txt["nv"] == "1"
    assert txt["pt"] == bn.pairing_tag("tok") != bn.pairing_tag("other token")
    assert "pt" not in bn.build_txt("a" * 32, "x", False, None) and bn.build_txt("a" * 32, "x", False, None)["acc"] == "0"


def test_sealed_answer_opens_only_untampered_and_only_for_its_request() -> None:
    asker, answerer = X25519PrivateKey.generate(), X25519PrivateKey.generate()
    sealed = bn.seal_token(answerer, asker.public_key().public_bytes_raw(), "rid-1",
                           {"token": "T", "device_id": "dev", "label": "A"})
    assert bn.open_token(asker, "rid-1", sealed)["token"] == "T"
    with pytest.raises(PairingError, match="tampered"):
        bn.open_token(asker, "rid-2", sealed)
    flipped = dict(sealed, sealed=_b64(bytes([base64.urlsafe_b64decode(sealed["sealed"])[0] ^ 1])
                                       + base64.urlsafe_b64decode(sealed["sealed"])[1:]))
    with pytest.raises(PairingError, match="tampered"):
        bn.open_token(asker, "rid-1", flipped)
    with pytest.raises(PairingError, match="tampered"):
        bn.open_token(X25519PrivateKey.generate(), "rid-1", sealed)


def test_a_request_waits_for_a_person_and_never_pairs_on_its_own(tmp_path: Path) -> None:
    clock = _Clock()
    nearby = _service(tmp_path, clock)
    answer = nearby.door_post(bn.REQUEST, _request_body(), "10.0.0.2")
    assert answer["expires_in_s"] == bn.REQUEST_TTL_S
    assert nearby.pairing.record is None, "if a request pairs before Accept then anyone nearby pairs at will"
    with nearby._lock:
        live = nearby._live_incoming()
    assert [r.label for r in live] == ["Laptop B"]
    clock.now += bn.REQUEST_TTL_S + 1
    with pytest.raises(PairingError) as expired:
        nearby.accept(answer["request_id"])
    assert expired.value.code == "no_request" and nearby.pairing.record is None


def test_decline_removes_the_request_so_it_can_never_be_accepted(tmp_path: Path) -> None:
    nearby = _service(tmp_path, _Clock())
    rid = nearby.door_post(bn.REQUEST, _request_body(), "10.0.0.2")["request_id"]
    declined = nearby.decline(rid)  # the asker's door is not running: told is False, still declined
    assert declined == {"declined": True, "asker_told": False, "from_label": "Laptop B"}
    with pytest.raises(PairingError) as gone:
        nearby.accept(rid)
    assert gone.value.code == "no_request" and nearby.pairing.record is None


def test_rate_limits_per_laptop_pending_cap_and_not_accepting(tmp_path: Path) -> None:
    clock = _Clock()
    nearby = _service(tmp_path, clock)
    first = nearby.door_post(bn.REQUEST, _request_body(), "10.0.0.2")
    with pytest.raises(PairingError) as waiting:
        nearby.door_post(bn.REQUEST, _request_body(), "10.0.0.2")
    assert waiting.value.code == "request_pending" and waiting.value.status == 429
    nearby.door_post(bn.CANCEL, {"request_id": first["request_id"]}, "10.0.0.2")
    second = nearby.door_post(bn.REQUEST, _request_body(), "10.0.0.2")
    nearby.door_post(bn.CANCEL, {"request_id": second["request_id"]}, "10.0.0.9")  # not the asker: ignored
    with nearby._lock:
        assert len(nearby._live_incoming()) == 1
    nearby.door_post(bn.CANCEL, {"request_id": second["request_id"]}, "10.0.0.2")
    nearby.door_post(bn.CANCEL, {"request_id": nearby.door_post(bn.REQUEST, _request_body(), "10.0.0.2")["request_id"]},
                     "10.0.0.2")
    with pytest.raises(PairingError) as limited:
        nearby.door_post(bn.REQUEST, _request_body(), "10.0.0.2")
    assert limited.value.code == "rate_limited" and limited.value.status == 429
    clock.now += bn.RATE_WINDOW_S + 1
    nearby.door_post(bn.REQUEST, _request_body(), "10.0.0.2")  # the window passed: allowed again
    for i in range(3, 3 + bn.MAX_PENDING - 1):
        nearby.door_post(bn.REQUEST, _request_body(), f"10.0.0.{i}")
    with pytest.raises(PairingError) as busy:
        nearby.door_post(bn.REQUEST, _request_body(), "10.0.0.99")
    assert busy.value.code == "busy"
    nearby.accepting = False
    with pytest.raises(PairingError) as closed:
        nearby.door_post(bn.REQUEST, _request_body(), "10.0.0.100")
    assert closed.value.code == "not_accepting" and closed.value.status == 403


def test_reconnect_needs_the_same_token_and_a_fresh_challenge(tmp_path: Path) -> None:
    nearby = _service(tmp_path, _Clock())
    door = "10.0.0.1:40000"  # this laptop's door as the asker dialed it

    def proof(token: str, nonce: str, side: bytes = b"asker") -> str:
        return bn.reconnect_proof(token, side, nonce, nearby.pairing.device_id, door, "b" * 32, 40002)

    nonce = nearby.door_get(bn.CHALLENGE)["nonce"]
    body = {"nonce": nonce, "device_id": "b" * 32, "label": "Laptop B", "door_port": 40002, "proof": proof("shared", nonce)}
    with pytest.raises(PairingError) as unknown:
        nearby.door_post(bn.RECONNECT, body, "10.0.0.2", door)
    assert unknown.value.code == "not_remembered", "if a laptop with no pairing honors a reconnect then anyone pairs"
    nearby.pairing.adopt(PairingRecord(peer_device_id="old", peer_label="Laptop B", peer_host="10.0.0.1",
                                       peer_door_port=40000, token="shared", door_port=0, paired_at="t", role="inviter"))
    nonce = nearby.door_get(bn.CHALLENGE)["nonce"]
    forged = dict(body, nonce=nonce, proof=proof("not the token", nonce))
    with pytest.raises(PairingError) as wrong:
        nearby.door_post(bn.RECONNECT, forged, "10.0.0.2", door)
    assert wrong.value.code == "not_remembered"
    nonce = nearby.door_get(bn.CHALLENGE)["nonce"]
    good = dict(body, nonce=nonce, proof=proof("shared", nonce))
    answer = nearby.door_post(bn.RECONNECT, good, "10.0.0.2", door)
    assert answer["proof"] == proof("shared", nonce, b"answerer")
    record = nearby.pairing.record
    assert record is not None and (record.peer_host, record.peer_door_port) == ("10.0.0.2", 40002)
    with pytest.raises(PairingError) as replay:
        nearby.door_post(bn.RECONNECT, good, "10.0.0.66", door)
    assert replay.value.code == "bad_challenge", "if a captured reconnect replays then its sender redirects the token"
    nearby.pairing.shutdown()


@pytest.mark.requirement("B2BPAIR-02")
def test_dns_sd_lines_before_10_am_parse() -> None:
    """dns-sd pads hours under 10 with a space (`` 0:04:14.100``); those lines must still parse."""
    stype = "_odjq1._tcp"
    browse = [" 0:04:14.100  Add        3  33 local.               _odjq1._tcp.     Open DJ Laptop A abcdef n",
              "10:04:14.100  Add        3  33 local.               _odjq1._tcp.     Open DJ Laptop B abcdef n"]
    assert bn.b2b_mdns.parse_browse(browse, stype) == ["Open DJ Laptop A abcdef n", "Open DJ Laptop B abcdef n"]
    address = [" 0:04:25.402  Add  40000003      13  host.local.                192.168.1.20                                 4500"]
    assert [ip for ip, _dev in bn.b2b_mdns.parse_addresses(address)] == ["192.168.1.20"]
