"""Art-Net 4 ArtDmx, ArtSync, and discovery-layer Poll codecs (LIGHT-21 / discovery).

Clean-room from the public Art-Net 4 specification (art-net.org.uk). Pure byte building:
no sockets (L24). ArtPoll encode and soft ArtPollReply decode for a future UDP discovery
loop live here; strict ``parse_artpollreply`` and the subscriber table stay in
``artnet_poll`` (LIGHT-10462).

Port-Address is 15 bits: Net (7 bits) : Sub-Net (4 bits) : Universe (4 bits). Art-Net 4
deprecates Port-Address 0, so it is refused.
"""

from __future__ import annotations

from dataclasses import dataclass

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
MAX_PORT_ADDRESS = 0x7FFF
MAX_SEQUENCE = 255
SEQUENCE_OFF = 0


# ----------------------------------------------------------------------------- helpers


def _header(opcode: int) -> bytes:
    """ID, OpCode (low byte first) and ProtVer (high byte first)."""
    return ARTNET_ID + opcode.to_bytes(2, "little") + PROTOCOL_VERSION.to_bytes(2, "big")


def _require_range(name: str, value: int, low: int, high: int) -> None:
    if not low <= value <= high:
        raise ValueError(f"{name} must be {low}..{high}, got {value}")


def _require_address_field(name: str, value: int, high: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer, got {value!r}")  # noqa: TRY004 - address validation uses ValueError
    _require_range(name, value, 0, high)


# ----------------------------------------------------------------------------- public api


def port_address(net: int, subnet: int, universe: int) -> int:
    """Pack 7:4:4 integers; raise ValueError for non-integers, booleans or invalid ranges."""
    _require_address_field("net", net, MAX_NET)
    _require_address_field("subnet", subnet, MAX_SUBNET)
    _require_address_field("universe", universe, MAX_UNIVERSE)
    return (net << 8) | (subnet << 4) | universe


def unpack_port_address(address: int) -> tuple[int, int, int]:
    """Return (net, subnet, universe); raise ValueError unless address is an integer 0..32767."""
    _require_address_field("port_address", address, MAX_PORT_ADDRESS)
    return address >> 8, (address >> 4) & MAX_SUBNET, address & MAX_UNIVERSE


def next_sequence(sequence: int) -> int:
    """Sequence after ``sequence``: 1..255 then wraps to 1, never 0 (0 means sequencing off)."""
    _require_range("sequence", sequence, SEQUENCE_OFF, MAX_SEQUENCE)
    return 1 if sequence >= MAX_SEQUENCE else sequence + 1


def encode_artdmx(port_address: int, data: bytes, sequence: int, physical: int = 0) -> bytes:
    """One ArtDmx packet. Odd-length data is padded with a zero slot to the even length Art-Net requires."""
    if port_address == 0:
        raise ValueError("Port-Address 0 is deprecated in Art-Net 4; use universe 1 or higher")
    _require_range("port_address", port_address, 1, MAX_PORT_ADDRESS)
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


# ----------------------------------------------------------------------------- discovery


@dataclass(frozen=True)
class PollReplyView:
    """Soft decode view of one ArtPollReply for node listing (discovery only)."""

    ip: str
    short_name: str
    universe: int


def encode_artpoll(*, reply_on_change: bool = True) -> bytes:
    """One ArtPoll packet (OpCode 0x2000) for discovery; delegates to ``artnet_poll``."""
    from apps.lighting import artnet_poll

    return artnet_poll.encode_artpoll(reply_on_change=reply_on_change)


def decode_pollreply(data: bytes) -> PollReplyView | None:
    """Parse ArtPollReply bytes into ip, short name and first output universe, or None.

    Wrong ID, wrong opcode, truncate, ArtDmx, and replies with no output ports all yield
    ``None``. Subscription code should keep using ``artnet_poll.parse_artpollreply``.
    """
    from apps.lighting import artnet_poll

    try:
        node = artnet_poll.parse_artpollreply(data)
    except ValueError:
        return None
    if not node.output_port_addresses:
        return None
    return PollReplyView(
        ip=node.ip,
        short_name=node.short_name,
        universe=node.output_port_addresses[0],
    )
