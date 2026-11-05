"""B2BNEAR-06: a Nearby scan always finishes, inside a deadline, and says when it is incomplete.

[if] a Nearby scan outlives its deadline or reports an incomplete scan as complete [then] fail, [else stop].

Seen Mon 12 Oct 2026 on a loaded Mac (load 9, memory pressure warn): ``/api/v1/b2b/nearby``
sat on ``scanning: true`` with ``scanned_at: null`` and no ``scan_error`` for minutes, while
``dns-sd -B`` by hand listed both laptops. Every test here drives REAL subprocesses through
``fake_dns_sd.py`` (a stand-in for ``/usr/bin/dns-sd`` that prints its line formats and then
sleeps like the real client); the deadlines themselves are never stubbed.

- [if] a scan thread dies on an exception and the snapshot still says scanning with no error [then] fail
- [if] a scan that hangs is reported as scanning forever instead of "scan timed out after N s" [then] fail
- [if] a late finish of an abandoned scan overwrites the timeout verdict [then] fail
- [if] dns-sd that starts slower than the browse window reads as "nobody there" [then] fail
- [if] dns-sd that never prints is an empty list instead of an error [then] fail
- [if] one silent instance stalls the scan or hides the instances that did answer [then] fail
- [if] resolves run one after another so N instances cost N windows [then] fail
- [if] a dns-sd child that ignores SIGTERM raises out of the scan or is left running [then] fail
- [if] a partial scan that missed the partner reads `partner_listed: false` [then] fail
- [if] the CLI gives up on `list` before the engine's own scan deadline [then] fail
"""

from __future__ import annotations

import os
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

from apps.ableton_link.link_transports import classify_interface, parse_hardware_ports
from apps.b2b_pairing import b2b_mdns
from apps.b2b_pairing import b2b_nearby as bn
from apps.b2b_pairing.b2b_service import PairingService

pytestmark = [pytest.mark.requirement("B2BNEAR-06")]  # the path-naming block adds B2BNEAR-02

STYPE = "_odjtest._tcp"
FAKE = Path(__file__).with_name("fake_dns_sd.py")
#: Process start and Python import on a busy CI host; generous, still far under any deadline.
SLACK_S = 1.5


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def fake_dns_sd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point b2b_mdns at the stand-in dns-sd; returns the dir each run writes its pid into."""
    wrapper = tmp_path / "dns-sd"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    pids = tmp_path / "pids"
    pids.mkdir()
    monkeypatch.setattr(b2b_mdns, "DNS_SD", str(wrapper))
    monkeypatch.setenv(b2b_mdns.SERVICE_TYPE_ENV, STYPE)
    monkeypatch.setenv("FAKE_DNSSD_PIDS", str(pids))
    for knob in ("FAKE_DNSSD_START_DELAY", "FAKE_DNSSD_INSTANCES", "FAKE_DNSSD_HANG", "FAKE_DNSSD_RESOLVE_DELAY",
                 "FAKE_DNSSD_IGNORE_TERM"):
        monkeypatch.delenv(knob, raising=False)
    assert b2b_mdns.backend() == "dns-sd", "control: the stand-in must be the backend under test"
    return pids


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _service(tmp_path: Path, hardware_ports, clock=time.monotonic) -> bn.NearbyService:
    pairing = PairingService(tmp_path, engine_port=lambda: 1, label="Laptop A", set_peer=lambda _c: None,
                             hardware_ports=dict)
    nearby = bn.NearbyService(pairing, tmp_path, hardware_ports, clock=clock)
    pairing.nearby = nearby
    return nearby


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


# ---------------------------------------------------------------- b2b_mdns.scan


def test_slow_start_still_lists_everyone_and_notes_the_load(fake_dns_sd: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The Air at 17:14Z: dns-sd took longer to start than the 1.5 s browse window lasted."""
    start_delay = b2b_mdns.BROWSE_S + 0.5
    monkeypatch.setenv("FAKE_DNSSD_START_DELAY", str(start_delay))
    monkeypatch.setenv("FAKE_DNSSD_INSTANCES", "Open DJ A,Open DJ B")
    result = b2b_mdns.scan()
    assert sorted(r.instance for r in result.resolved) == ["Open DJ A", "Open DJ B"], (
        "if dns-sd that starts slower than the browse window reads as nobody there then fail"
    )
    assert result.unresolved == []
    assert result.notes and "to start" in result.notes[0], "a loaded machine must say its list may be incomplete"


def test_fast_start_has_no_note(fake_dns_sd: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Control for the note: an idle machine's complete scan carries none."""
    monkeypatch.setenv("FAKE_DNSSD_INSTANCES", "Open DJ A")
    result = b2b_mdns.scan()
    assert [r.instance for r in result.resolved] == ["Open DJ A"] and result.notes == [] and not result.partial


def test_dns_sd_that_never_prints_is_a_timeout_error_inside_the_budget(
    fake_dns_sd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_DNSSD_START_DELAY", "600")
    began = time.monotonic()
    with pytest.raises(b2b_mdns.ScanTimeout, match="printed nothing"):
        b2b_mdns.scan()
    assert time.monotonic() - began < b2b_mdns.STARTUP_S + SLACK_S, "if the startup wait is unbounded then fail"


def test_a_silent_instance_is_partial_and_the_rest_still_list(fake_dns_sd: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_DNSSD_INSTANCES", "Open DJ A,Open DJ Silent,Open DJ B")
    monkeypatch.setenv("FAKE_DNSSD_HANG", "Open DJ Silent")
    began = time.monotonic()
    result = b2b_mdns.scan()
    assert sorted(r.instance for r in result.resolved) == ["Open DJ A", "Open DJ B"]
    assert result.unresolved == ["Open DJ Silent"] and result.partial
    assert any("1 of 3" in note for note in result.notes)
    assert time.monotonic() - began < b2b_mdns.SCAN_BUDGET_S + SLACK_S


def test_resolves_run_in_parallel(fake_dns_sd: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Six instances whose resolve each takes 1 s: one after another would be 6 s and blow the budget."""
    names = [f"Open DJ {i}" for i in range(6)]
    monkeypatch.setenv("FAKE_DNSSD_INSTANCES", ",".join(names))
    monkeypatch.setenv("FAKE_DNSSD_RESOLVE_DELAY", "1.0")
    began = time.monotonic()
    result = b2b_mdns.scan()
    took = time.monotonic() - began
    assert sorted(r.instance for r in result.resolved) == names and not result.partial
    assert took < b2b_mdns.BROWSE_S + 1.0 + b2b_mdns.ADDRESS_S + 2 * SLACK_S, f"resolves took {took:.1f} s: serial?"


def test_a_child_that_ignores_sigterm_is_killed_not_raised(fake_dns_sd: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``proc.wait(timeout=2)`` after SIGTERM used to raise TimeoutExpired, which killed the scan thread."""
    monkeypatch.setenv("FAKE_DNSSD_IGNORE_TERM", "1")
    monkeypatch.setenv("FAKE_DNSSD_INSTANCES", "Open DJ A")
    result = b2b_mdns.scan()
    assert [r.instance for r in result.resolved] == ["Open DJ A"]
    pids = [int(p.name) for p in fake_dns_sd.iterdir()]
    assert len(pids) == 3, f"control: browse, resolve and address each ran once, saw {pids}"
    assert not [pid for pid in pids if _alive(pid)], "if a dns-sd child outlives the scan then fail"


# ---------------------------------------------------------------- NearbyService


def test_a_scan_thread_that_raises_reports_scan_error(fake_dns_sd: Path, tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    """networksetup timing out on a loaded Mac raised out of the thread: scanning forever, no error."""
    monkeypatch.setenv("FAKE_DNSSD_INSTANCES", "Open DJ A")

    def hardware_ports() -> dict[str, str]:
        raise RuntimeError("networksetup -listallhardwareports timed out")

    nearby = _service(tmp_path, hardware_ports)
    try:
        view = nearby.snapshot(wait=True)
        assert view["scanning"] is False
        assert view["scan_error"] is not None and "networksetup" in view["scan_error"], view
        assert view["scanned_at"] is not None, "a scan that ended in error still ended"
    finally:
        nearby.stop()
        nearby.pairing.shutdown()


def test_a_hung_scan_times_out_and_its_late_finish_is_ignored(fake_dns_sd: Path, tmp_path: Path,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_DNSSD_INSTANCES", "Open DJ A")
    release = threading.Event()

    def hardware_ports() -> dict[str, str]:
        release.wait(30)
        return {"lo": "Ethernet"}

    clock = _Clock()
    nearby = _service(tmp_path, hardware_ports, clock)
    try:
        first = nearby.snapshot()
        assert first["scanning"] is True and first["scan_error"] is None
        stuck = nearby._scan_thread
        assert stuck is not None
        clock.now += bn.SCAN_DEADLINE_S - 0.1
        assert nearby.snapshot()["scanning"] is True, "if a scan is abandoned before its deadline then fail"
        clock.now += 0.2
        timed_out = nearby.snapshot()
        assert timed_out["scanning"] is False, "if a hung scan reads scanning forever then fail"
        assert timed_out["scan_error"] == f"scan timed out after {bn.SCAN_DEADLINE_S:g} s"
        assert nearby.discovery(kick=False)["partner_listed"] is None
        release.set()
        stuck.join(10)
        assert not stuck.is_alive()
        assert nearby.snapshot()["scan_error"] == timed_out["scan_error"], "a late finish overwrote the timeout"
        clock.now += bn.SCAN_MAX_AGE_S + 0.1
        fresh = nearby.snapshot(wait=True)
        assert fresh["scan_error"] is None and [p["label"] for p in fresh["nearby"]] == ["Open DJ A"], fresh
        assert fresh["nearby"][0]["path"] == "wired" and fresh["nearby"][0]["via"] == "lo"
    finally:
        release.set()
        nearby.stop()
        nearby.pairing.shutdown()


def test_wait_returns_by_the_deadline_with_a_real_clock(fake_dns_sd: Path, tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bn, "SCAN_DEADLINE_S", 1.0)
    release = threading.Event()
    nearby = _service(tmp_path, lambda: (release.wait(30), {})[1])
    try:
        began = time.monotonic()
        view = nearby.snapshot(wait=True)
        assert time.monotonic() - began < 1.0 + SLACK_S
        assert view["scanning"] is False and view["scan_error"] == "scan timed out after 1 s"
    finally:
        release.set()
        nearby.stop()
        nearby.pairing.shutdown()


def test_a_partial_scan_reports_its_note(fake_dns_sd: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_DNSSD_INSTANCES", "Open DJ A,Open DJ Silent")
    monkeypatch.setenv("FAKE_DNSSD_HANG", "Open DJ Silent")
    nearby = _service(tmp_path, lambda: {"lo": "Ethernet"})
    try:
        view = nearby.snapshot(wait=True)
        assert [p["label"] for p in view["nearby"]] == ["Open DJ A"]
        assert view["scan_error"] is None and view["scan_note"] is not None and "1 of 2" in view["scan_note"]
        assert nearby.discovery(kick=False)["partner_listed"] is None, (
            "if a partial scan that missed the partner reads as not listed then fail"
        )
    finally:
        nearby.stop()
        nearby.pairing.shutdown()


def test_the_scan_deadline_covers_the_whole_mdns_budget() -> None:
    assert bn.SCAN_DEADLINE_S > b2b_mdns.SCAN_BUDGET_S
    assert b2b_mdns.SCAN_BUDGET_S > b2b_mdns.STARTUP_S + b2b_mdns.BROWSE_S + b2b_mdns.RESOLVE_S + b2b_mdns.ADDRESS_S, (
        "a loaded machine that starts slowly must still have time to resolve"
    )


# ---------------------------------------------------------------- path naming (B2BNEAR-02)

#: ``networksetup -listallhardwareports`` captured read-only on demon-llama (Mac14,3) and the
#: Air (Mac15,13), Mon 12 Oct 2026. demon-llama's en0 is Ethernet with no link (status
#: inactive); it was on Wi-Fi (en1). The Air's en0 IS its Wi-Fi.
DEMON_LLAMA_PORTS = """
Hardware Port: Ethernet
Device: en0
Ethernet Address: 00:00:00:00:00:01

Hardware Port: Ethernet Adapter (en4)
Device: en4
Ethernet Address: 00:00:00:00:00:02

Hardware Port: Thunderbolt Bridge
Device: bridge0
Ethernet Address: 00:00:00:00:00:03

Hardware Port: Wi-Fi
Device: en1
Ethernet Address: 00:00:00:00:00:04
"""
AIR_PORTS = """
Hardware Port: Thunderbolt Bridge
Device: bridge0
Ethernet Address: 00:00:00:00:00:05

Hardware Port: Wi-Fi
Device: en0
Ethernet Address: 00:00:00:00:00:06
"""


@pytest.mark.requirement("B2BNEAR-02")
@pytest.mark.parametrize(("ports", "device", "kind"), [
    (DEMON_LLAMA_PORTS, "en0", "wired"),
    (DEMON_LLAMA_PORTS, "en1", "wifi"),
    (DEMON_LLAMA_PORTS, "bridge0", "bridge"),
    (AIR_PORTS, "en0", "wifi"),
])
def test_path_kind_follows_the_hardware_port_not_the_device_name(ports: str, device: str, kind: str) -> None:
    """``en0`` is Ethernet on a Mac mini/Studio and Wi-Fi on a MacBook; only the port name decides."""
    mapping = parse_hardware_ports(ports)
    assert classify_interface(device, mapping.get(device)) == kind
