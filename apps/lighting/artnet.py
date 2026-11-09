"""Art-Net 4 ArtDmx and ArtSync packet encoder (LIGHT-21 in the lighting spec).

Clean-room from the public Art-Net 4 specification (art-net.org.uk). Pure byte building:
no sockets (L24) and no ArtPoll discovery (L22).

Port-Address is 15 bits: Net (7 bits) : Sub-Net (4 bits) : Universe (4 bits). Art-Net 4
deprecates Port-Address 0, so it is refused.
"""

from __future__ import annotations

ARTNET_ID = b"Art-Net\x00"
PROTOCOL_VERSION = 14
OPCODE_ARTDMX = 0x5000
OPCODE_ARTSYNC = 0x5200
ARTDMX_HEADER_LENGTH = 18
ARTSYNC_LENGTH = 14
MIN_DMX_SLOTS = 2
MAX_DMX_SLOTS = 512
MAX_NET = 127
MAX_SUBNET = 15
MAX_UNIVERSE = 15
MAX_SEQUENCE = 255
SEQUENCE_OFF = 0


# ----------------------------------------------------------------------------- helpers


def _header(opcode: int) -> bytes:
    """ID, OpCode (low byte first) and ProtVer (high byte first)."""
    return ARTNET_ID + opcode.to_bytes(2, "little") + PROTOCOL_VERSION.to_bytes(2, "big")


def _require_range(name: str, value: int, low: int, high: int) -> None:
    if not low <= value <= high:
        raise ValueError(f"{name} must be {low}..{high}, got {value}")


# ----------------------------------------------------------------------------- public api


def port_address(net: int, subnet: int, universe: int) -> int:
    """15-bit Port-Address from Net 0..127, Sub-Net 0..15 and Universe 0..15."""
    _require_range("net", net, 0, MAX_NET)
    _require_range("subnet", subnet, 0, MAX_SUBNET)
    _require_range("universe", universe, 0, MAX_UNIVERSE)
    return (net << 8) | (subnet << 4) | universe


def next_sequence(sequence: int) -> int:
    """Sequence after ``sequence``: 1..255 then wraps to 1, never 0 (0 means sequencing off)."""
    _require_range("sequence", sequence, SEQUENCE_OFF, MAX_SEQUENCE)
    return 1 if sequence >= MAX_SEQUENCE else sequence + 1


def encode_artdmx(port_address: int, data: bytes, sequence: int, physical: int = 0) -> bytes:
    """One ArtDmx packet. Odd-length data is padded with a zero slot to the even length Art-Net requires."""
    if port_address == 0:
        raise ValueError("Port-Address 0 is deprecated in Art-Net 4; use universe 1 or higher")
    _require_range("port_address", port_address, 1, 0x7FFF)
    _require_range("sequence", sequence, SEQUENCE_OFF, MAX_SEQUENCE)
    _require_range("physical", physical, 0, 255)
    slots = bytes(data)
    if len(slots) > MAX_DMX_SLOTS:
        raise ValueError(f"ArtDmx carries at most {MAX_DMX_SLOTS} slots, got {len(slots)}")
    if len(slots) % 2:
        slots += b"\x00"
    if len(slots) < MIN_DMX_SLOTS:
        raise ValueError(f"ArtDmx carries at least {MIN_DMX_SLOTS} slots, got {len(data)}")
    return (
        _header(OPCODE_ARTDMX)
        + bytes((sequence, physical, port_address & 0xFF, port_address >> 8))
        + len(slots).to_bytes(2, "big")
        + slots
    )


def encode_artsync() -> bytes:
    """One ArtSync packet: header plus two zero Aux bytes."""
    return _header(OPCODE_ARTSYNC) + b"\x00\x00"
