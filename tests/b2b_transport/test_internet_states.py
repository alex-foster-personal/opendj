"""B2BNET-10 and B2BNET-12: what the internet pairing is doing right now, named, with the invite's deadline.

[if] the engine reports an internet pairing [then] it names one state and why, [else stop].

The pure half (``derive_state``) is a table test. The rest runs two REAL ``InternetService``
objects against the REAL local relay (workerd via ``wrangler dev --local``): nothing is mocked,
and the one fault (relay down) is the relay process being stopped.

- [if] an invite POST to a relay nobody listens on raises anything but a named relay_unreachable 502 [then] fail
- [if] an unset relay URL reads as any state but unconfigured, or its reason does not name MDT_B2B_RELAY_URL [then] fail
- [if] an open invite does not carry the relay's own deadline (expires_at UTC, expires_in_s) [then] fail
- [if] an invite the relay time-boxed reads as anything but expired, or its code still pairs [then] fail
- [if] two paired services do not both read connected with a connected_since [then] fail
- [if] stopping the relay leaves a paired service reading connected, or unnamed [then] fail
"""

from __future__ import annotations

import socket
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from apps.b2b_pairing.b2b_pairing import PairingError
from apps.b2b_pairing.b2b_service import PairingService
from apps.b2b_transport.internet import RELAY_URL_ENV, InternetInvite, join_over_relay
from apps.b2b_transport.internet_service import InternetService, derive_state
from tests.b2b_transport.relay_harness import local_relay, relay_or_skip

pytestmark = [pytest.mark.requirement("B2BNET-10"), pytest.mark.requirement("B2BNET-12")]

DEVICE_A = "a" * 32
#: Production INVITE_TTL_MS ((private path)), in seconds.
INVITE_TTL_S = 43_200
#: The short time-box the expiry test runs its own relay with.
SHORT_TTL_MS = 8_000


def _closed_port() -> int:
    """A loopback port nothing listens on: bound, read, released."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _service(state_dir: Path, label: str) -> InternetService:
    state_dir.mkdir(parents=True, exist_ok=True)
    lan = PairingService(state_dir, engine_port=lambda: 1, label=label, set_peer=lambda _c: None, hardware_ports=dict)
    return InternetService(state_dir=state_dir, lan=lan, engine_url=lambda: "http://127.0.0.1:1")


def _wait_for(read: Any, want: str, budget_s: float) -> dict[str, Any]:
    deadline = time.monotonic() + budget_s
    status = read()
    while status["state"] != want and time.monotonic() < deadline:
        time.sleep(0.25)
        status = read()
    assert status["state"] == want, f"wanted {want!r} within {budget_s}s, last status: {status}"
    return status


# ---------------------------------------------------------------- pure: the state table


def _status(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"relay_url": "https://relay.example", "relay_error": None, "paired": False,
                            "invite": None, "channel": None, "restore_error": None, "peer_label": None}
    return {**base, **over}


def _channel(**over: Any) -> dict[str, Any]:
    base = {"connected": False, "peer_present": False, "relay_connected": False, "last_error": None}
    return {**base, **over}


@pytest.mark.parametrize(
    ("status", "state", "reason_has"),
    [
        (_status(relay_url=None, relay_error="Internet B2B needs a relay: set MDT_B2B_RELAY_URL to its URL"),
         "unconfigured", "MDT_B2B_RELAY_URL"),
        (_status(), "idle", ""),
        (_status(invite={"state": "open", "open": True, "error": None}), "inviting", ""),
        (_status(invite={"state": "expired", "open": False, "error": "invite expired"}), "expired", "expired"),
        (_status(invite={"state": "burned", "open": False, "error": "5 wrong tries"}), "burned", "5 wrong tries"),
        (_status(invite={"state": "relay_lost", "open": False, "error": "the relay closed the invite"}),
         "relay_unreachable", "relay closed"),
        (_status(paired=True, channel=_channel(connected=True, peer_present=True, relay_connected=True)), "connected", ""),
        (_status(paired=True, channel=_channel(relay_connected=True)), "waiting_for_partner", ""),
        (_status(paired=True, channel=_channel(last_error="relay_unreachable: cannot reach the B2B relay: ConnectionRefusedError")),
         "relay_unreachable", "ConnectionRefusedError"),
        (_status(paired=True, channel=_channel()), "connecting", ""),
        (_status(paired=True, channel=None, restore_error="relay_unconfigured: set MDT_B2B_RELAY_URL"),
         "relay_unreachable", "MDT_B2B_RELAY_URL"),
    ],
)
def test_derive_state_names_every_case(status: dict[str, Any], state: str, reason_has: str) -> None:
    got, reason = derive_state(status)
    assert got == state, (status, got, reason)
    assert reason_has in (reason or ""), (state, reason)


def test_a_connected_channel_wins_over_a_stale_error() -> None:
    """Mutation control in the other direction: last_error is history, a live session is now."""
    status = _status(paired=True, channel=_channel(connected=True, peer_present=True, relay_connected=True,
                                                   last_error="relay connection closed: earlier"))
    assert derive_state(status)[0] == "connected"


# ---------------------------------------------------------------- no relay at all


def test_invite_against_a_relay_nobody_listens_on_is_a_named_relay_unreachable() -> None:
    with pytest.raises(PairingError) as refused:
        InternetInvite(f"http://127.0.0.1:{_closed_port()}", DEVICE_A, "A", lambda _p: None)
    assert (refused.value.code, refused.value.status) == ("relay_unreachable", 502), refused.value
    assert "cannot reach the B2B relay" in str(refused.value)


def test_unset_relay_url_reads_unconfigured_and_names_the_setting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(RELAY_URL_ENV, raising=False)
    status = _service(tmp_path / "a", "A").status()
    assert status["state"] == "unconfigured", status
    assert RELAY_URL_ENV in status["state_reason"], status
    monkeypatch.setenv(RELAY_URL_ENV, "http://127.0.0.1:1")
    assert _service(tmp_path / "b", "B").status()["state"] == "idle", "positive control: a set URL is not unconfigured"


# ---------------------------------------------------------------- against the real relay


@pytest.fixture(scope="module")
def relay(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    with local_relay(tmp_path_factory.mktemp("relay")) as base:
        yield base


@relay_or_skip()
def test_an_open_invite_carries_the_relays_deadline(relay: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(RELAY_URL_ENV, relay)
    service = _service(tmp_path / "a", "A")
    try:
        invite = service.create_invite()
        assert INVITE_TTL_S - 60 <= invite["expires_in_s"] <= INVITE_TTL_S, invite
        expires_at = datetime.fromisoformat(invite["expires_at"])
        assert expires_at.tzinfo is not None and expires_at.utcoffset() == timedelta(0), invite["expires_at"]
        drift = abs((expires_at - datetime.now(UTC)).total_seconds() - invite["expires_in_s"])
        assert drift < 5, f"expires_at and expires_in_s disagree by {drift}s"
        assert service.status()["state"] == "inviting"
    finally:
        service.shutdown()


@relay_or_skip()
def test_an_invite_the_relay_time_boxed_reads_expired_and_its_code_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with local_relay(tmp_path / "relay", relay_vars={"INVITE_TTL_MS": str(SHORT_TTL_MS)}) as base:
        monkeypatch.setenv(RELAY_URL_ENV, base)
        service = _service(tmp_path / "a", "A")
        try:
            invite = service.create_invite()
            assert invite["expires_in_s"] <= SHORT_TTL_MS / 1000, f"the relay var did not take: {invite}"
            status = _wait_for(service.status, "expired", SHORT_TTL_MS / 1000 + 20)
            assert "expired" in status["state_reason"], status
            with pytest.raises(PairingError) as refused:
                join_over_relay(base, invite["code"])
            assert (refused.value.code, refused.value.status) == ("invite_closed", 410), refused.value
        finally:
            service.shutdown()


@relay_or_skip()
def test_only_the_relays_expiry_reason_reads_expired(relay: str) -> None:
    """Overshoot control: a relay close for any other reason is relay_lost, never expired."""
    for reason, want in (("invite expired", "expired"), ("nameplate reallocated", "relay_lost")):
        invite = InternetInvite(relay, DEVICE_A, "A", lambda _p: None)
        try:
            invite._answer({"t": "closed", "reason": reason})
            assert (invite.state, invite.error) == (want, reason), (reason, invite.status())
        finally:
            invite.cancel()


@relay_or_skip()
def test_paired_services_read_connected_then_relay_unreachable_when_the_relay_stops(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    a, b = _service(tmp_path / "a", "Laptop A"), _service(tmp_path / "b", "Laptop B")
    try:
        with local_relay(tmp_path / "relay") as base:
            monkeypatch.setenv(RELAY_URL_ENV, base)
            code = a.create_invite()["code"]
            b.accept(code)
            for service in (a, b):
                status = _wait_for(service.status, "connected", 20)
                since = datetime.fromisoformat(status["channel"]["connected_since"])
                assert since.utcoffset() == timedelta(0) and since <= datetime.now(UTC), status
        # The relay process is gone now (context exited): both must say so by name.
        for service in (a, b):
            status = _wait_for(service.status, "relay_unreachable", 30)
            assert status["channel"]["connected"] is False and status["channel"]["relay_connected"] is False, status
            assert status["state_reason"], status
    finally:
        a.shutdown()
        b.shutdown()


@relay_or_skip()
@pytest.mark.requirement("B2BNET-16")
def test_a_relay_upgrade_budget_refusal_reads_relay_unreachable_with_its_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Four upgrades: inviter, joiner, then each side's room socket. The next reconnect is over budget."""
    a, b = _service(tmp_path / "a", "Laptop A"), _service(tmp_path / "b", "Laptop B")
    try:
        with local_relay(tmp_path / "relay", relay_vars={"UPGRADES_PER_WINDOW": "4"}) as base:
            monkeypatch.setenv(RELAY_URL_ENV, base)
            b.accept(a.create_invite()["code"])
            for service in (a, b):
                _wait_for(service.status, "connected", 20)
            b.shutdown()
            b.restore()
            status = _wait_for(b.status, "relay_unreachable", 30)
            assert "upgrade_rate_limited" in status["state_reason"], status
            assert a.status()["channel"]["relay_connected"] is True, "control: A's socket was admitted and stays"
    finally:
        a.shutdown()
        b.shutdown()
