"""LIGHT-70 lighting network monitor (issue #9877).

[if] the Art-Net or sACN monitor misreads a frame or prints unchanged or non-JSON lines [then] fail, [else stop].
[if] the E1.31 or Art-Net decoders misread the fixtures, accept a bad ACN identifier, or the
listener prints unchanged channels or non-JSON lines under --json [then] fail, [else stop].
"""

from __future__ import annotations

import asyncio
import json
import socket
import struct
from pathlib import Path

import pytest

from apps.lighting.monitor import (
    MonitorConfig,
    PacketError,
    changed_channels,
    decode_artdmx,
    decode_artpollreply_header,
    decode_e131_data,
    sacn_multicast_group,
    start_monitor,
)
from apps.opendj_cli import lighting_cli

pytestmark = [pytest.mark.requirement("LIGHT-9947")]

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _artdmx(universe: int, sequence: int, slots: bytes) -> bytes:
    return (
        b"Art-Net\x00"
        + struct.pack("<H", 0x5000)
        + bytes([0, 14, sequence, 0, universe & 0xFF, universe >> 8])
        + struct.pack(">H", len(slots))
        + slots
    )


# -----------------------------------------------------------------------------
# decoders


def test_e131_fixture_decodes_universe_priority_and_512_slots() -> None:
    """[if] the recorded E1.31 fixture is decoded [then] universe 1, priority 100, 512 slots."""
    frame = decode_e131_data(_fixture("e131_universe1_full.bin"))
    assert (frame.universe, frame.priority, len(frame.slots)) == (1, 100, 512)
    assert frame.sequence == 17 and frame.options == 0
    assert frame.source_name == "opendj fixture"


def test_wrong_acn_identifier_raises_naming_field_and_offset() -> None:
    """[if] a packet has a wrong ACN identifier [then] decode raises naming field and offset."""
    packet = bytearray(_fixture("e131_universe1_full.bin"))
    packet[4:16] = b"ASC-E1.99\x00\x00\x00"
    with pytest.raises(PacketError, match=r"ACN Packet Identifier at offset 4") as caught:
        decode_e131_data(bytes(packet))
    assert caught.value.field_name == "ACN Packet Identifier" and caught.value.offset == 4


@pytest.mark.parametrize(
    ("offset", "value", "field"),
    [
        (18, 0x09, "Root Vector"),
        (43, 0x09, "Framing Vector"),
        (117, 0x09, "DMP Vector"),
        (124, 0x09, "Property value count"),
    ],
)
def test_e131_bad_vectors_and_lengths_name_their_field(offset: int, value: int, field: str) -> None:
    """[if] a vector or count is wrong [then] the error names that field."""
    packet = bytearray(_fixture("e131_universe1_full.bin"))
    packet[offset] = value
    with pytest.raises(PacketError, match=field):
        decode_e131_data(bytes(packet))


def test_e131_truncated_packet_fails_on_the_root_length() -> None:
    """[if] the packet is cut short [then] a flags-and-length field mismatch is raised."""
    with pytest.raises(PacketError, match="Root Flags and Length length at offset 16"):
        decode_e131_data(_fixture("e131_universe1_full.bin")[:-10])


def test_artdmx_fixture_decodes_port_address_and_slots() -> None:
    """[if] the Art-Net fixture is decoded [then] universe 1, sequence 9, 512 slots."""
    frame = decode_artdmx(_fixture("artdmx_universe1.bin"))
    assert (frame.protocol, frame.universe, frame.sequence, len(frame.slots)) == ("artnet", 1, 9, 512)
    assert decode_artdmx(_artdmx(0x0123, 1, b"\x01\x02")).universe == 0x0123


def test_artdmx_wrong_opcode_names_the_opcode() -> None:
    """[if] the opcode is not OpDmx [then] the error names OpCode at offset 8."""
    packet = bytearray(_fixture("artdmx_universe1.bin"))
    packet[9] = 0x21
    with pytest.raises(PacketError, match="OpCode at offset 8"):
        decode_artdmx(bytes(packet))


def test_artpollreply_header_fields() -> None:
    """[if] an OpPollReply is decoded [then] ip, port and names come back."""
    packet = bytearray(239)
    packet[0:8] = b"Art-Net\x00"
    struct.pack_into("<H", packet, 8, 0x2100)
    packet[10:14] = bytes([10, 0, 0, 7])
    struct.pack_into("<H", packet, 14, 6454)
    packet[26:31] = b"Node1"
    packet[44:53] = b"Long Node"
    struct.pack_into(">H", packet, 172, 4)
    header = decode_artpollreply_header(bytes(packet))
    assert (header.ip, header.port, header.short_name, header.long_name, header.num_ports) == (
        "10.0.0.7",
        6454,
        "Node1",
        "Long Node",
        4,
    )


def test_changed_channels_and_multicast_group() -> None:
    """[if] only channel 3 moves [then] only channel 3 is reported; universe 1 maps to 239.255.0.1."""
    assert changed_channels(b"\x00\x00\x05", b"\x00\x00\x09") == {3: 9}
    assert sacn_multicast_group(1) == "239.255.0.1"
    assert sacn_multicast_group(0x0102) == "239.255.1.2"


# -----------------------------------------------------------------------------
# listener


async def _send_two_frames_and_collect(as_json: bool) -> list[str]:
    lines: list[str] = []
    monitor = await start_monitor(
        MonitorConfig(host="127.0.0.1", artnet_port=0, sacn_port=None, as_json=as_json), lines.append
    )
    try:
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        target = ("127.0.0.1", monitor.ports["artnet"])
        sender.sendto(_artdmx(1, 1, bytes([255, 0, 0, 0])), target)
        await _wait_for(lines, 1)
        sender.sendto(_artdmx(1, 2, bytes([255, 0, 128, 0])), target)
        await _wait_for(lines, 2)
        sender.close()
    finally:
        monitor.close()
    return lines


async def _wait_for(lines: list[str], count: int) -> None:
    for _ in range(200):
        if len(lines) >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"monitor printed {len(lines)} lines, expected {count}: {lines}")


def test_listener_prints_two_lines_with_changed_channels_only() -> None:
    """[if] two Art-Net frames arrive on 127.0.0.1 [then] two lines with the changed channels only."""
    lines = asyncio.run(_send_two_frames_and_collect(as_json=False))
    assert lines == ["artnet universe 1 seq 1: 1=255", "artnet universe 1 seq 2: 3=128"]


def test_json_flag_prints_one_json_object_per_line() -> None:
    """[if] --json is passed [then] each line is a JSON object."""
    lines = asyncio.run(_send_two_frames_and_collect(as_json=True))
    documents = [json.loads(line) for line in lines]
    assert all(isinstance(document, dict) for document in documents)
    assert [document["changed"] for document in documents] == [{"1": 255}, {"3": 128}]
    assert documents[1]["universe"] == 1 and documents[1]["sequence"] == 2


def test_cli_protocol_flags_select_listeners() -> None:
    """[if] neither --artnet nor --sacn is given [then] both listen; --json sets JSON output."""
    parser = lighting_cli._parser()
    both = lighting_cli.config_from_args(parser.parse_args(["monitor"]), as_json=False)
    assert both.artnet_port == 6454 and both.sacn_port == 5568 and not both.as_json
    only_sacn = lighting_cli.config_from_args(parser.parse_args(["monitor", "--sacn", "--json"]), as_json=False)
    assert only_sacn.artnet_port is None and only_sacn.sacn_port == 5568 and only_sacn.as_json
