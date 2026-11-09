"""LIGHT-70: network monitor that decodes Art-Net and sACN packets and prints per-universe changes.

A receiver written from the Art-Net 4 specification and ANSI E1.31-2018 byte layouts, sharing no
code with the lighting senders, so tests and users can check what actually went on the wire.

- ``decode_artdmx``: Art-Net ``OpDmx`` (0x5000) to universe, sequence, physical port and slots.
- ``decode_artpollreply_header``: the fixed header of ``OpPollReply`` (0x2100).
- ``decode_e131_data``: an E1.31 data packet, validating the ACN identifier, every layer vector
  and every flags-and-length field; returns universe, priority, sequence, options and slots.
- ``start_monitor``: an asyncio UDP listener on a configurable address (Art-Net 6454, sACN 5568)
  that calls ``emit`` with one line per frame naming only the channels that changed.

Every decode error is a ``PacketError`` naming the field and its byte offset.
"""

from __future__ import annotations

import asyncio
import json
import socket
import struct
from collections.abc import Callable
from dataclasses import dataclass, field

ARTNET_PORT = 6454
SACN_PORT = 5568

ARTNET_ID = b"Art-Net\x00"
OP_DMX = 0x5000
OP_POLL_REPLY = 0x2100
ARTDMX_HEADER_LEN = 18
ARTPOLLREPLY_HEADER_LEN = 174

ACN_PACKET_IDENTIFIER = b"ASC-E1.17\x00\x00\x00"
E131_PREAMBLE_SIZE = 0x0010
VECTOR_ROOT_E131_DATA = 0x00000004
VECTOR_E131_DATA_PACKET = 0x00000002
VECTOR_DMP_SET_PROPERTY = 0x02
DMP_ADDRESS_DATA_TYPE = 0xA1
E131_HEADER_LEN = 126  # through the DMX start code
E131_ROOT_PDU_OFFSET = 16
E131_FRAMING_PDU_OFFSET = 38
E131_DMP_PDU_OFFSET = 115
MAX_SLOTS = 512

SACN_MULTICAST_PREFIX = (239, 255)


class PacketError(ValueError):
    """A packet that does not match its specification, naming the field and byte offset."""

    def __init__(self, field_name: str, offset: int, detail: str) -> None:
        super().__init__(f"{field_name} at offset {offset}: {detail}")
        self.field_name = field_name
        self.offset = offset


@dataclass(frozen=True)
class DmxFrame:
    """One decoded DMX frame from either protocol."""

    protocol: str
    universe: int
    sequence: int
    slots: bytes
    priority: int | None = None
    options: int | None = None
    physical: int | None = None
    source_name: str | None = None


@dataclass(frozen=True)
class ArtPollReplyHeader:
    """The fixed fields at the start of an Art-Net ``OpPollReply``."""

    ip: str
    port: int
    version: int
    net_switch: int
    sub_switch: int
    oem: int
    esta_manufacturer: int
    short_name: str
    long_name: str
    num_ports: int


# -----------------------------------------------------------------------------
# helpers


def _require_length(data: bytes, minimum: int, what: str) -> None:
    if len(data) < minimum:
        raise PacketError(what, len(data), f"packet is {len(data)} bytes, needs at least {minimum}")


def _expect(field_name: str, offset: int, actual: int, expected: int) -> None:
    if actual != expected:
        raise PacketError(field_name, offset, f"expected 0x{expected:x}, got 0x{actual:x}")


def _artnet_opcode(data: bytes, expected: int, minimum: int) -> None:
    _require_length(data, minimum, "Art-Net header")
    if data[:8] != ARTNET_ID:
        raise PacketError("ID", 0, f"expected {ARTNET_ID!r}, got {data[:8]!r}")
    _expect("OpCode", 8, struct.unpack_from("<H", data, 8)[0], expected)


def _c_string(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace")


def _flags_and_length(data: bytes, offset: int, field_name: str) -> None:
    value = struct.unpack_from(">H", data, offset)[0]
    _expect(f"{field_name} flags", offset, value >> 12, 0x7)
    _expect(f"{field_name} length", offset, value & 0x0FFF, len(data) - offset)


def sacn_multicast_group(universe: int) -> str:
    """The E1.31 multicast address a universe is sent to (239.255.hi.lo)."""
    if not 1 <= universe <= 63999:
        raise ValueError(f"sACN universe must be 1..63999, got {universe}")
    return f"{SACN_MULTICAST_PREFIX[0]}.{SACN_MULTICAST_PREFIX[1]}.{universe >> 8}.{universe & 0xFF}"


# -----------------------------------------------------------------------------
# decoders


def decode_artdmx(data: bytes) -> DmxFrame:
    """Decode an Art-Net ``OpDmx`` packet; the universe is the 15-bit Port-Address."""
    _artnet_opcode(data, OP_DMX, ARTDMX_HEADER_LEN)
    sequence, physical, sub_uni, net = data[12], data[13], data[14], data[15]
    if net & 0x80:
        raise PacketError("Net", 15, f"top bit must be zero, got 0x{net:x}")
    length = struct.unpack_from(">H", data, 16)[0]
    if not 2 <= length <= MAX_SLOTS:
        raise PacketError("Length", 16, f"must be 2..{MAX_SLOTS}, got {length}")
    if len(data) < ARTDMX_HEADER_LEN + length:
        raise PacketError(
            "Data", ARTDMX_HEADER_LEN, f"Length says {length} slots, packet carries {len(data) - ARTDMX_HEADER_LEN}"
        )
    slots = bytes(data[ARTDMX_HEADER_LEN : ARTDMX_HEADER_LEN + length])
    return DmxFrame(protocol="artnet", universe=(net << 8) | sub_uni, sequence=sequence, slots=slots, physical=physical)


def decode_artpollreply_header(data: bytes) -> ArtPollReplyHeader:
    """Decode the fixed header of an Art-Net ``OpPollReply`` (through NumPorts)."""
    _artnet_opcode(data, OP_POLL_REPLY, ARTPOLLREPLY_HEADER_LEN)
    return ArtPollReplyHeader(
        ip=socket.inet_ntoa(data[10:14]),
        port=struct.unpack_from("<H", data, 14)[0],
        version=struct.unpack_from(">H", data, 16)[0],
        net_switch=data[18],
        sub_switch=data[19],
        oem=struct.unpack_from(">H", data, 20)[0],
        esta_manufacturer=struct.unpack_from("<H", data, 24)[0],
        short_name=_c_string(data[26:44]),
        long_name=_c_string(data[44:108]),
        num_ports=struct.unpack_from(">H", data, 172)[0],
    )


def decode_e131_data(data: bytes) -> DmxFrame:
    """Decode an E1.31 data packet, validating identifier, vectors and every length field."""
    _require_length(data, E131_HEADER_LEN, "E1.31 packet")
    _expect("Preamble Size", 0, struct.unpack_from(">H", data, 0)[0], E131_PREAMBLE_SIZE)
    _expect("Post-amble Size", 2, struct.unpack_from(">H", data, 2)[0], 0)
    if data[4:16] != ACN_PACKET_IDENTIFIER:
        raise PacketError("ACN Packet Identifier", 4, f"expected {ACN_PACKET_IDENTIFIER!r}, got {data[4:16]!r}")
    _flags_and_length(data, E131_ROOT_PDU_OFFSET, "Root Flags and Length")
    _expect("Root Vector", 18, struct.unpack_from(">I", data, 18)[0], VECTOR_ROOT_E131_DATA)
    _flags_and_length(data, E131_FRAMING_PDU_OFFSET, "Framing Flags and Length")
    _expect("Framing Vector", 40, struct.unpack_from(">I", data, 40)[0], VECTOR_E131_DATA_PACKET)
    universe = struct.unpack_from(">H", data, 113)[0]
    if not 1 <= universe <= 63999:
        raise PacketError("Universe", 113, f"must be 1..63999, got {universe}")
    _flags_and_length(data, E131_DMP_PDU_OFFSET, "DMP Flags and Length")
    _expect("DMP Vector", 117, data[117], VECTOR_DMP_SET_PROPERTY)
    _expect("Address Type & Data Type", 118, data[118], DMP_ADDRESS_DATA_TYPE)
    _expect("First Property Address", 119, struct.unpack_from(">H", data, 119)[0], 0)
    _expect("Address Increment", 121, struct.unpack_from(">H", data, 121)[0], 1)
    count = struct.unpack_from(">H", data, 123)[0]
    if not 1 <= count <= MAX_SLOTS + 1:
        raise PacketError("Property value count", 123, f"must be 1..{MAX_SLOTS + 1}, got {count}")
    _expect("Property value count", 123, count, len(data) - (E131_HEADER_LEN - 1))
    _expect("START Code", 125, data[125], 0)
    return DmxFrame(
        protocol="sacn",
        universe=universe,
        sequence=data[111],
        slots=bytes(data[E131_HEADER_LEN:]),
        priority=data[108],
        options=data[112],
        source_name=_c_string(data[44:108]),
    )


# -----------------------------------------------------------------------------
# change tracking and formatting


def changed_channels(previous: bytes, current: bytes) -> dict[int, int]:
    """1-based channel -> new value for every slot that differs (a missing slot reads as 0)."""
    width = max(len(previous), len(current))
    old = previous.ljust(width, b"\x00")
    new = current.ljust(width, b"\x00")
    return {index + 1: new[index] for index in range(width) if old[index] != new[index]}


def format_frame(frame: DmxFrame, changed: dict[int, int], as_json: bool) -> str:
    """One output line for a frame: text, or a JSON object when ``as_json``."""
    if as_json:
        document = {
            "protocol": frame.protocol,
            "universe": frame.universe,
            "sequence": frame.sequence,
            "priority": frame.priority,
            "slots": len(frame.slots),
            "changed": {str(channel): value for channel, value in changed.items()},
        }
        return json.dumps(document, separators=(",", ":"))
    body = " ".join(f"{channel}={value}" for channel, value in changed.items()) or "(no change)"
    return f"{frame.protocol} universe {frame.universe} seq {frame.sequence}: {body}"


@dataclass
class UniverseTracker:
    """Last slots seen per (protocol, universe); turns frames into change lines."""

    as_json: bool
    last: dict[tuple[str, int], bytes] = field(default_factory=dict)

    def line_for(self, frame: DmxFrame) -> str:
        key = (frame.protocol, frame.universe)
        changed = changed_channels(self.last.get(key, b""), frame.slots)
        self.last[key] = frame.slots
        return format_frame(frame, changed, self.as_json)


# -----------------------------------------------------------------------------
# listener


@dataclass(frozen=True)
class MonitorConfig:
    """Where to listen. A port of ``None`` turns that protocol off; 0 picks a free port."""

    host: str = "0.0.0.0"
    artnet_port: int | None = ARTNET_PORT
    sacn_port: int | None = SACN_PORT
    sacn_universes: tuple[int, ...] = ()
    as_json: bool = False


def _artnet_dmx_or_none(data: bytes) -> DmxFrame | None:
    """ArtDmx frames decode; other Art-Net opcodes (polls, replies, timecode) are skipped."""
    is_other_opcode = len(data) >= 10 and data[:8] == ARTNET_ID and struct.unpack_from("<H", data, 8)[0] != OP_DMX
    return None if is_other_opcode else decode_artdmx(data)


Decoder = Callable[[bytes], DmxFrame | None]


class _FrameProtocol(asyncio.DatagramProtocol):
    def __init__(self, decode: Decoder, tracker: UniverseTracker, emit: Callable[[str], None]) -> None:
        self._decode = decode
        self._tracker = tracker
        self._emit = emit

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            frame = self._decode(data)
        except PacketError as error:
            self._emit(f"error from {addr[0]}:{addr[1]}: {error}")
            return
        if frame is not None:
            self._emit(self._tracker.line_for(frame))


@dataclass
class Monitor:
    """A running listener; ``ports`` maps protocol to the bound port."""

    transports: list[asyncio.DatagramTransport]
    ports: dict[str, int]

    def close(self) -> None:
        for transport in self.transports:
            transport.close()


def _bound_socket(host: str, port: int, groups: tuple[str, ...]) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    for group in groups:
        membership = socket.inet_aton(group) + socket.inet_aton("0.0.0.0")
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
    return sock


async def start_monitor(config: MonitorConfig, emit: Callable[[str], None]) -> Monitor:
    """Bind the configured listeners and start calling ``emit`` with one line per frame."""
    if config.artnet_port is None and config.sacn_port is None:
        raise ValueError("start_monitor needs at least one of artnet_port or sacn_port")
    loop = asyncio.get_running_loop()
    tracker = UniverseTracker(as_json=config.as_json)
    monitor = Monitor(transports=[], ports={})
    listeners: list[tuple[str, int, Decoder, tuple[str, ...]]] = []
    if config.artnet_port is not None:
        listeners.append(("artnet", config.artnet_port, _artnet_dmx_or_none, ()))
    if config.sacn_port is not None:
        groups = tuple(sacn_multicast_group(universe) for universe in config.sacn_universes)
        listeners.append(("sacn", config.sacn_port, decode_e131_data, groups))
    for name, port, decode, groups in listeners:
        sock = _bound_socket(config.host, port, groups)
        transport, _ = await loop.create_datagram_endpoint(lambda d=decode: _FrameProtocol(d, tracker, emit), sock=sock)
        monitor.transports.append(transport)
        monitor.ports[name] = sock.getsockname()[1]
    return monitor
