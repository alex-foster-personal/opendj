"""B2BPAIR-09: the Linux (Avahi) half of the B2B mDNS layer and the Linux hardware-port census.

The parser cases are Avahi's own output, captured from ``avahi-browse -p -r -t`` on nucbox
(Ubuntu 26.04, avahi 0.8) on Mon 12 Oct 2026. The live cases run the real Avahi clients
where they are installed and refuse to report a pass where they are not.

- [if] an escaped instance name (``\\032`` space, ``\\.`` dot) is not decoded [then] fail
- [if] the same instance answered on two interfaces becomes two records [then] fail
- [if] an IPv6 answer or another service type leaks into the result [then] fail
- [if] avahi-browse with no reachable daemon returns an empty list instead of raising [then] fail
- [if] a wireless device is not "Wi-Fi", or loopback / a tunnel gets a port name [then] fail
- [if] Linux Link host time is not CLOCK_MONOTONIC_RAW in microseconds (what Carabiner stamps) [then] fail
- [if] Linux reads Avahi answers, sysfs and the host clock [then] records, ports and Link time are exact, [else stop]
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

import pytest

from apps.ableton_link.link_clock import link_now_us
from apps.ableton_link.link_transports import classify_interface, linux_hardware_ports
from apps.b2b_pairing import b2b_mdns

pytestmark = [pytest.mark.requirement("B2BPAIR-09")]

STYPE = "_odjtx._tcp"
CAPTURED = [
    r"+;odjlan0;IPv4;Open\032DJ\032Laptop\.A;_odjtx._tcp;local",
    r'=;odjlan0;IPv4;Open\032DJ\032Laptop\.A;_odjtx._tcp;local;linuxhost.local;10.77.0.1;4321;"id=abc" "v=1"',
]


def test_captured_avahi_answer_parses_to_one_resolved_record() -> None:
    found = b2b_mdns.parse_avahi_resolved(CAPTURED, STYPE)
    assert found == [
        b2b_mdns.Resolved(
            "Open DJ Laptop.A", "linuxhost.local", 4321, {"id": "abc", "v": "1"}, [("10.77.0.1", "odjlan0")]
        )
    ]


def test_one_instance_on_two_interfaces_is_one_record_with_two_addresses() -> None:
    lines = [*CAPTURED, CAPTURED[1].replace("odjlan0", "eth9").replace("10.77.0.1", "10.88.0.1")]
    found = b2b_mdns.parse_avahi_resolved(lines, STYPE)
    assert len(found) == 1
    assert found[0].addresses == [("10.77.0.1", "odjlan0"), ("10.88.0.1", "eth9")]


def test_ipv6_answers_and_other_types_are_dropped() -> None:
    v6 = CAPTURED[1].replace(";IPv4;", ";IPv6;").replace("10.77.0.1", "fe80::1")
    other = CAPTURED[1].replace("_odjtx._tcp", "_other._tcp")
    assert b2b_mdns.parse_avahi_resolved([v6, other], STYPE) == []
    # Control: the same line, untouched, does parse, so the drop above is the filter and not a broken parser.
    assert len(b2b_mdns.parse_avahi_resolved([CAPTURED[1]], STYPE)) == 1


@pytest.mark.requirement("B2BREADDR-03")
def test_an_ipv6_address_on_an_ipv4_answer_is_dropped() -> None:
    """Avahi can resolve a service browsed over IPv4 to the host's IPv6 address (seen on the rig,
    Wed 14 Oct 2026: ``fe80::431:eff:fe3f:29f8``). The door is IPv4 only and ``http://fe80::..:port``
    is not a URL, so such a line is not an address of the instance."""
    lines = [
        r'=;odjveth0;IPv4;Open\032DJ\032B;_odjtx._tcp;local;b.local;fe80::431:eff:fe3f:29f8;4321;"id=abc" "v=1"',
        r'=;odjveth0;IPv4;Open\032DJ\032B;_odjtx._tcp;local;b.local;10.77.0.2;4321;"id=abc" "v=1"',
        r'=;odjveth0;IPv4;Open\032DJ\032C;_odjtx._tcp;local;c.local;fe80::1;4322;"id=def" "v=1"',
    ]
    found = {r.instance: r.addresses for r in b2b_mdns.parse_avahi_resolved(lines, "_odjtx._tcp")}
    assert found == {"Open DJ B": [("10.77.0.2", "odjveth0")], "Open DJ C": []}, found



@pytest.mark.skipif(
    shutil.which("avahi-browse") is None, reason="UNAVAILABLE: avahi-browse not installed here. Not a pass."
)
def test_avahi_browse_without_a_daemon_raises_with_avahis_own_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DBUS_SYSTEM_BUS_ADDRESS", f"unix:path={tmp_path / 'no-bus'}")
    with pytest.raises(b2b_mdns.MdnsUnavailable, match="avahi-browse exited"):
        b2b_mdns._avahi_browse_and_resolve(STYPE, time.monotonic() + b2b_mdns.SCAN_BUDGET_S)


@pytest.mark.skipif(
    shutil.which("avahi-publish") is None, reason="UNAVAILABLE: avahi-publish not installed here. Not a pass."
)
def test_avahi_publish_without_a_daemon_raises_instead_of_a_silent_dead_advert(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DBUS_SYSTEM_BUS_ADDRESS", f"unix:path={tmp_path / 'no-bus'}")
    with pytest.raises(b2b_mdns.MdnsUnavailable, match="did not register"):
        b2b_mdns._avahi_publish(["avahi-publish", "-s", "x", STYPE, "4321"])


def _fake_sysfs_device(root: Path, name: str, arphrd: str, *, wireless: bool = False) -> None:
    device = root / name
    device.mkdir()
    (device / "type").write_text(f"{arphrd}\n")
    if wireless:
        (device / "wireless").mkdir()


def test_linux_hardware_ports_names_wifi_and_ethernet_and_skips_loopback_and_tunnels(tmp_path: Path) -> None:
    _fake_sysfs_device(tmp_path, "lo", "772")
    _fake_sysfs_device(tmp_path, "wlan0", "1", wireless=True)
    _fake_sysfs_device(tmp_path, "eth0", "1")
    _fake_sysfs_device(tmp_path, "tailscale0", "65534")
    ports = linux_hardware_ports(tmp_path)
    assert ports == {"eth0": "Ethernet", "wlan0": "Wi-Fi"}
    assert classify_interface("eth0", ports.get("eth0")) == "wired"
    assert classify_interface("wlan0", ports.get("wlan0")) == "wifi"
    assert classify_interface("lo", ports.get("lo")) is None


@pytest.mark.skipif(not Path("/sys/class/net/lo").exists(), reason="UNAVAILABLE: no Linux sysfs here. Not a pass.")
def test_linux_hardware_ports_reads_this_hosts_real_sysfs() -> None:
    ports = linux_hardware_ports()
    assert "lo" not in ports
    assert all(kind in ("Wi-Fi", "Ethernet") for kind in ports.values()), ports


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="UNAVAILABLE: Linux Link host clock, not this platform. Not a pass."
)
def test_linux_link_host_time_is_clock_monotonic_raw_in_microseconds() -> None:
    before = time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW) // 1000
    now = link_now_us()
    after = time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW) // 1000
    assert before <= now <= after, (before, now, after)
