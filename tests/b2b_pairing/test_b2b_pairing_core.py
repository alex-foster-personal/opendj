"""B2BPAIR-01..03 pure core: invite code, expiry, attempt limit, SPAKE2 handshake, sealed token.

No network and no engine: the inviter and joiner halves talk to each other in-process,
which is the exact byte exchange the LAN door carries (tests/webui/test_b2b_pairing_two_engines.py
runs the same exchange between two real engines).

[if] a joiner types the open code [then] both sides hold one sealed token; a wrong or spent code pairs none, [else stop]
"""

from __future__ import annotations

import re

import pytest

from apps.b2b_pairing import b2b_pairing as bp
from apps.shared.private_files import is_owner_only

pytestmark = [pytest.mark.requirement("B2BPAIR-01"), pytest.mark.requirement("B2BPAIR-02"), pytest.mark.requirement("B2BPAIR-03")]


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _joiner_info() -> dict[str, object]:
    return {"device_id": "joiner-dev", "label": "Joiner", "door_port": 40001}


def _pair(invite: bp.Invite, typed: str) -> bp.JoinOutcome:
    joiner = bp.JoinerHandshake(typed)
    start = invite.start(joiner.message(), _joiner_info(), source_ip="192.0.2.7")
    confirm_a = joiner.check_inviter(start["msg_b"], start["confirm_b"])
    sealed = invite.finish(start["session"], confirm_a)
    return joiner.open_sealed(sealed)


def test_code_is_short_and_uses_only_the_unambiguous_alphabet() -> None:
    codes = {bp.generate_code() for _ in range(400)}
    assert len(codes) > 390, "if codes repeat this often then the generator is broken"
    for code in codes:
        assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{3}-[0-9A-HJKMNP-TV-Z]{3}", code), code
        assert not set(code) & set("ILOU"), f"if {code} holds I/L/O/U then the alphabet is ambiguous"


def test_typed_code_normalizes_case_separators_and_lookalikes() -> None:
    assert bp.normalize_code(" k7q-4mx ") == "K7Q4MX"
    assert bp.normalize_code("o1l-iab") == "011" + "1AB"
    with pytest.raises(bp.PairingError) as bad:
        bp.normalize_code("K7Q4M")
    assert bad.value.code == "bad_code_format"
    with pytest.raises(bp.PairingError):
        bp.normalize_code("K7Q4MU")


def test_right_code_pairs_and_both_sides_hold_the_same_token() -> None:
    clock = _Clock()
    invite = bp.Invite.create(device_id="inviter-dev", label="Inviter", clock=clock)
    outcome = _pair(invite, invite.code.lower())
    assert invite.paired is not None
    assert outcome.token == invite.paired.token
    assert len(outcome.token) >= 40
    assert outcome.inviter_label == "Inviter"
    assert invite.paired.peer_host == "192.0.2.7" and invite.paired.peer_door_port == 40001
    assert not invite.is_open(), "if the invite stays open after pairing then a second joiner can pair"


def test_wrong_code_is_rejected_and_counts_as_an_attempt() -> None:
    invite = bp.Invite.create(device_id="inviter-dev", label="Inviter", clock=_Clock())
    wrong = "222-222" if invite.code != "222-222" else "333-333"
    with pytest.raises(bp.PairingError) as refused:
        _pair(invite, wrong)
    assert refused.value.code == "wrong_code"
    assert invite.attempts_left == bp.MAX_ATTEMPTS - 1
    assert invite.paired is None
    # The right code still works after one miss.
    assert _pair(invite, invite.code).token == invite.paired.token  # type: ignore[union-attr]


@pytest.mark.requirement("B2BPAIR-08")
def test_attempts_are_limited_then_the_invite_burns() -> None:
    invite = bp.Invite.create(device_id="inviter-dev", label="Inviter", clock=_Clock())
    wrong = "222-222" if invite.code != "222-222" else "333-333"
    for _ in range(bp.MAX_ATTEMPTS):
        with pytest.raises(bp.PairingError):
            _pair(invite, wrong)
    assert not invite.is_open()
    with pytest.raises(bp.PairingError) as burned:
        _pair(invite, invite.code)
    assert burned.value.code == "invite_closed", "if the right code works after the limit then guessing is unbounded"


@pytest.mark.requirement("B2BPAIR-08")
def test_an_invite_has_no_timer_it_still_pairs_after_a_long_gap() -> None:
    """[if] an open invite closes on the clock (it used to after 10 minutes) [then] fail."""
    clock = _Clock()
    invite = bp.Invite.create(device_id="inviter-dev", label="Inviter", clock=clock)
    clock.now += 7 * 24 * 3600.0  # a week of fake clock
    assert invite.is_open()
    outcome = _pair(invite, invite.code)
    assert outcome.token and invite.paired is not None and not invite.is_open(), "used: closed"


@pytest.mark.requirement("B2BPAIR-08")
def test_control_cancel_still_closes_an_invite() -> None:
    """[if] a cancelled invite still pairs (no timer must not mean no way to close it) [then] fail."""
    invite = bp.Invite.create(device_id="inviter-dev", label="Inviter", clock=_Clock())
    invite.cancelled = True
    assert not invite.is_open()
    with pytest.raises(bp.PairingError) as closed:
        _pair(invite, invite.code)
    assert closed.value.code == "invite_closed"


def test_a_tampered_sealed_token_does_not_open() -> None:
    invite = bp.Invite.create(device_id="inviter-dev", label="Inviter", clock=_Clock())
    joiner = bp.JoinerHandshake(invite.code)
    start = invite.start(joiner.message(), _joiner_info(), source_ip="192.0.2.7")
    sealed = invite.finish(start["session"], joiner.check_inviter(start["msg_b"], start["confirm_b"]))
    flipped = dict(sealed, sealed=sealed["sealed"][:-4] + ("AAAA" if not sealed["sealed"].endswith("AAAA") else "BBBB"))
    with pytest.raises(bp.PairingError) as bad:
        joiner.open_sealed(flipped)
    assert bad.value.code == "bad_seal"


def test_record_round_trips_through_disk_with_owner_only_mode(tmp_path) -> None:  # type: ignore[no-untyped-def]
    record = bp.PairingRecord(
        peer_device_id="p", peer_label="Peer", peer_host="192.0.2.9", peer_door_port=40002,
        token="t" * 43, door_port=40003, paired_at="2026-10-10T20:00:00Z", role="joiner",
    )
    path = tmp_path / "state" / bp.PAIRING_FILE
    bp.save_record(path, record)
    assert (path.stat().st_mode & 0o777) == 0o600
    assert is_owner_only(path)
    assert bp.load_record(path) == record
    assert record.peer_url == "http://192.0.2.9:40002"
    bp.forget_record(path)
    assert bp.load_record(path) is None


def test_candidate_addresses_prefer_bridge_then_wired_then_wifi() -> None:
    ranked = bp.rank_addresses(
        [("192.168.0.5", "wifi"), ("10.250.77.2", "bridge"), ("10.0.0.4", "wired"), ("198.51.100.9", "vpn"), ("127.0.0.1", None)]
    )
    assert [ip for ip, _ in ranked] == ["10.250.77.2", "10.0.0.4", "192.168.0.5"]


def test_inviter_refuses_a_joiner_that_skips_its_own_check() -> None:
    """A hostile joiner ignores the inviter's confirmation and calls finish anyway."""
    invite = bp.Invite.create(device_id="inviter-dev", label="Inviter", clock=_Clock())
    wrong = "222-222" if invite.code != "222-222" else "333-333"
    attacker = bp.JoinerHandshake(wrong)
    start = invite.start(attacker.message(), _joiner_info(), source_ip="192.0.2.66")
    forged = bp._confirm(b"\x00" * 32, b"joiner")
    with pytest.raises(bp.PairingError) as refused:
        invite.finish(start["session"], forged)
    assert refused.value.code == "wrong_code"
    assert invite.paired is None, "if finish pairs without a matching confirmation then anyone on the LAN can pair"
