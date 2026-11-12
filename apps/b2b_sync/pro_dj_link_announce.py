"""Read-only parser for Pro DJ Link device announce (keep-alive) packets.

Reimplemented from public protocol write-ups of the Pro DJ Link network. Each device
broadcasts a keep-alive on UDP port 50000 so peers know it is present. This module
only DECODES bytes the caller already has; it never opens a socket, transmits,
discovers or alters any peer.

Layout (offsets in bytes, multi-byte fields big-endian):

  0x00-0x09  magic ``Qspt1WmJOL``
  0x0a       packet type, ``0x06`` for a keep-alive announce
  0x0c-0x1f  device name, 20 bytes, NUL padded ASCII
  0x22-0x23  packet length, at least 0x36 (54)
  0x24       device (player) number
  0x26-0x2b  MAC address
  0x2c-0x2f  IPv4 address
  0x34       device kind byte (reported raw)

Bytes past offset 0x36 that the declared length covers are kept as raw
``extension_bytes`` and given no meaning. A short packet, a wrong magic or type, or
a declared length that disagrees with the packet raises ``AnnounceParseError``.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass

MAGIC: bytes = b"Qspt1WmJOL"
ANNOUNCE_PACKET_TYPE: int = 0x06
ANNOUNCE_BASE_LENGTH: int = 0x36
NAME_SLICE: slice = slice(0x0C, 0x20)
LENGTH_SLICE: slice = slice(0x22, 0x24)
DEVICE_NUMBER_OFFSET: int = 0x24
MAC_SLICE: slice = slice(0x26, 0x2C)
IP_SLICE: slice = slice(0x2C, 0x30)
DEVICE_KIND_OFFSET: int = 0x34


class AnnounceParseError(ValueError):
    """Bytes are not a well-formed Pro DJ Link announce packet."""


@dataclass(frozen=True)
class AnnounceEvent:
    """The fields one announce packet states, and any extension bytes left uninterpreted."""

    device_name: str
    device_number: int
    mac_address: str
    ip_address: str
    device_kind: int
    extension_bytes: bytes


def parse_announce(packet: bytes) -> AnnounceEvent:
    """Return the announce fields in ``packet``; raise ``AnnounceParseError`` when malformed."""
    if len(packet) < ANNOUNCE_BASE_LENGTH:
        raise AnnounceParseError(f"announce packet is {len(packet)} bytes, needs {ANNOUNCE_BASE_LENGTH}")
    if packet[: len(MAGIC)] != MAGIC:
        raise AnnounceParseError("packet does not start with the Pro DJ Link magic")
    if packet[0x0A] != ANNOUNCE_PACKET_TYPE:
        raise AnnounceParseError(f"packet type 0x{packet[0x0A]:02x} is not an announce (0x06)")
    declared = int.from_bytes(packet[LENGTH_SLICE], "big")
    if declared != len(packet):
        raise AnnounceParseError(f"declared length {declared} does not match packet length {len(packet)}")
    raw_name = packet[NAME_SLICE].split(b"\x00", 1)[0]
    try:
        name = raw_name.decode("ascii")
    except UnicodeDecodeError as error:
        raise AnnounceParseError("device name is not ASCII") from error
    if not name:
        raise AnnounceParseError("device name is empty")
    return AnnounceEvent(
        device_name=name,
        device_number=packet[DEVICE_NUMBER_OFFSET],
        mac_address=":".join(f"{byte:02x}" for byte in packet[MAC_SLICE]),
        ip_address=str(ipaddress.IPv4Address(packet[IP_SLICE])),
        device_kind=packet[DEVICE_KIND_OFFSET],
        extension_bytes=bytes(packet[ANNOUNCE_BASE_LENGTH:]),
    )
