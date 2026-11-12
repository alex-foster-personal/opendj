"""Passive Pro DJ Link announce packet parser (OPEN-12248).

[if] an announce packet is parsed [then] its fields decode and malformed bytes raise, [else stop].
"""

from __future__ import annotations

import ast
import socket
from pathlib import Path

import pytest

import apps.b2b_sync.pro_dj_link_announce as announce_module
from apps.b2b_sync.pro_dj_link_announce import AnnounceEvent, AnnounceParseError, parse_announce

pytestmark = pytest.mark.requirement("OPEN-12248")

FIXTURES: Path = Path(__file__).resolve().parent / "fixtures" / "pro_dj_link_announce"


def _fixture(name: str) -> bytes:
    """Synthesized packet bytes (see the fixture README)."""
    return bytes.fromhex((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("cdj_player2.hex", AnnounceEvent("CDJ-3000", 2, "c8:3d:fc:0a:12:34", "192.168.1.42", 0x01, b"")),
        ("mixer_33.hex", AnnounceEvent("DJM-900NXS2", 33, "00:e0:36:aa:bb:cc", "10.0.0.7", 0x02, b"")),
    ],
)
def test_fixture_fields_decode(name: str, expected: AnnounceEvent) -> None:
    """[if] a write-up-derived fixture is parsed [then] identity, model and address match its bytes, [else stop]."""
    packet = _fixture(name)
    event = parse_announce(packet)
    assert event == expected
    assert packet[0x24] == event.device_number
    assert packet[0x2C:0x30] == socket.inet_aton(event.ip_address)


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda p: p[:0x35], id="too-short"),
        pytest.param(lambda p: b"Xspt1WmJOL" + p[10:], id="bad-magic"),
        pytest.param(lambda p: p[:0x0A] + b"\x0a" + p[0x0B:], id="not-announce-type"),
        pytest.param(lambda p: p + b"\x00\x00", id="length-mismatch"),
        pytest.param(lambda p: b"", id="empty"),
    ],
)
def test_short_or_invalid_packets_raise(mutate) -> None:  # type: ignore[no-untyped-def]
    """[if] a packet is too short or has a bad signature [then] parsing raises, [else stop]."""
    with pytest.raises(AnnounceParseError):
        parse_announce(mutate(_fixture("cdj_player2.hex")))


def test_extension_bytes_stay_raw() -> None:
    """[if] an announce has unknown extension bytes [then] they stay raw with no invented fields, [else stop]."""
    event = parse_announce(_fixture("cdj_extension.hex"))
    assert event.extension_bytes == bytes.fromhex("deadbeef")
    assert set(vars(event)) == {
        "device_name",
        "device_number",
        "mac_address",
        "ip_address",
        "device_kind",
        "extension_bytes",
    }
    assert (event.device_name, event.device_number) == ("XDJ-XZ", 3)


def test_parsing_never_touches_a_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] parser tests run [then] no socket is opened or sent on, [else stop]."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("the announce parser must not open or send on a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    for name in ("cdj_player2.hex", "mixer_33.hex", "cdj_extension.hex"):
        parse_announce(_fixture(name))
    tree = ast.parse(Path(announce_module.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import | ast.ImportFrom) for alias in node.names
    } | {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not imported & {"socket", "asyncio", "socketserver"}
