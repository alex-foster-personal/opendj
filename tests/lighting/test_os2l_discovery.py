"""LIGHT-12 OS2L discovery (issue #9871).

[if] OS2L discovery misses an instance, returns a port-0 one, or does not name a missing tool [then] fail, [else stop].
[if] the dns-sd or avahi-browse output for two OS2L instances does not give two servers with
host and port, a port-0 instance is returned, or a missing tool is not named [then] fail,
[else stop].
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from apps.lighting import os2l_discovery
from apps.lighting.os2l_discovery import (
    Os2lDiscoveryUnavailable,
    Os2lServer,
    discover_os2l,
    parse_avahi_resolved,
    parse_dns_sd_browse,
    servers_from_dns_sd,
)

pytestmark = [pytest.mark.requirement("LIGHT-9961")]

FIXTURES = Path(__file__).parent / "fixtures"
EXPECTED = [Os2lServer("Light Rig", "lightpc.local", 4444), Os2lServer("Booth Desk 2", "booth-desk.local", 50123)]


def _lines(name: str) -> list[str]:
    return (FIXTURES / name).read_text().splitlines()


def test_dns_sd_browse_and_lookup_output_gives_two_servers() -> None:
    """[if] the dns-sd -B and -L output lists two instances [then] two results with host and port."""
    lines = _lines("dns_sd_browse_os2l.txt")
    assert parse_dns_sd_browse(lines) == ["Light Rig", "Booth Desk 2"]
    assert servers_from_dns_sd(lines, lines) == EXPECTED


def test_avahi_output_gives_two_servers() -> None:
    """[if] avahi-browse -p -r lists two instances [then] two results with host and port."""
    assert parse_avahi_resolved(_lines("avahi_browse_os2l.txt")) == EXPECTED


def test_neither_tool_on_path_names_both(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] neither DNS-SD tool exists on PATH [then] Os2lDiscoveryUnavailable names both."""
    monkeypatch.setattr(os2l_discovery.shutil, "which", lambda _tool: None)
    with pytest.raises(Os2lDiscoveryUnavailable, match=r"dns-sd.*avahi-browse"):
        discover_os2l(browse_s=0.1)


def test_port_zero_instance_is_dropped_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    """[if] an instance resolves to no port [then] it is dropped and logged by name."""
    lines = [line.replace("lightpc.local.:4444", "lightpc.local.:0") for line in _lines("dns_sd_browse_os2l.txt")]
    with caplog.at_level(logging.WARNING, logger=os2l_discovery.__name__):
        servers = servers_from_dns_sd(lines, lines)
    assert servers == EXPECTED[1:]
    assert "'Light Rig'" in caplog.text and "no port" in caplog.text
    avahi = [line.replace(";4444;", ";0;") for line in _lines("avahi_browse_os2l.txt")]
    assert parse_avahi_resolved(avahi) == EXPECTED[1:]


def test_unresolved_instance_is_dropped_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    """[if] a browsed instance never resolves [then] it is dropped and logged by name."""
    lines = _lines("dns_sd_browse_os2l.txt")
    browse = [line for line in lines if "can be reached" not in line or "Booth" in line]
    with caplog.at_level(logging.WARNING, logger=os2l_discovery.__name__):
        assert servers_from_dns_sd(browse, browse) == EXPECTED[1:]
    assert "'Light Rig' did not resolve" in caplog.text


@pytest.mark.skipif(os2l_discovery.available_tool() is None, reason="no dns-sd or avahi-browse on this host")
def test_live_browse_returns_a_list_without_port_zero() -> None:
    """[if] a DNS-SD tool exists [then] a live browse returns servers, none with port 0."""
    try:
        servers = discover_os2l(browse_s=0.5)
    except Os2lDiscoveryUnavailable as error:  # avahi-browse present but no daemon reachable
        pytest.skip(f"DNS-SD client present but unusable here: {error}")
    assert all(server.port > 0 for server in servers)
