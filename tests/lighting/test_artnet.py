"""LIGHT-9939: Art-Net 4 ArtDmx and ArtSync packet encoder with a 15-bit Port-Address.

[if] ArtDmx has a wrong header, padding or sequence wrap, accepts Port-Address 0 or 513 slots, or ArtSync is malformed [then] fail, [else stop].
"""

from __future__ import annotations

import pytest

from apps.lighting.artnet import (
    ARTDMX_HEADER_LENGTH,
    encode_artdmx,
    encode_artsync,
    next_sequence,
    port_address,
)

pytestmark = [pytest.mark.requirement("LIGHT-9939")]


def test_three_slots_pad_to_even_after_an_18_byte_header() -> None:
    """[if] port (0, 0, 1) with 3 slots is not 18 + 4 bytes with OpCode 0x00 0x50 [then] fail."""
    packet = encode_artdmx(port_address(0, 0, 1), b"\x01\x02\x03", sequence=1)
    assert len(packet) == ARTDMX_HEADER_LENGTH + 4
    assert packet[:8] == b"Art-Net\x00"
    assert packet[8:10] == b"\x00\x50"
    assert packet[10:12] == b"\x00\x0e"
    assert packet[14:16] == b"\x01\x00"
    assert packet[16:18] == b"\x00\x04"
    assert packet[18:] == b"\x01\x02\x03\x00"


def test_port_address_packs_net_high_byte_and_subuni_low_byte() -> None:
    """[if] net, subnet and universe are not packed 7:4:4 into SubUni and Net [then] fail."""
    address = port_address(127, 15, 15)
    assert address == 0x7FFF
    packet = encode_artdmx(address, bytes(512), sequence=0)
    assert packet[14] == 0xFF and packet[15] == 0x7F
    assert packet[16:18] == b"\x02\x00"
    with pytest.raises(ValueError):
        port_address(128, 0, 0)


def test_sequence_wraps_from_255_to_1_never_0() -> None:
    """[if] the sequence after 255 is 0 [then] fail."""
    assert next_sequence(255) == 1
    assert next_sequence(1) == 2
    assert encode_artdmx(1, b"\x00\x00", sequence=next_sequence(255))[12] == 1


def test_port_address_zero_is_refused_as_deprecated() -> None:
    """[if] Port-Address 0 encodes [then] fail."""
    with pytest.raises(ValueError, match="deprecated in Art-Net 4"):
        encode_artdmx(port_address(0, 0, 0), b"\x00\x00", sequence=1)


def test_513_slots_are_refused() -> None:
    """[if] 513 slots encode [then] fail."""
    with pytest.raises(ValueError):
        encode_artdmx(1, bytes(513), sequence=1)


def test_artsync_is_14_bytes_with_opcode_0x5200() -> None:
    """[if] ArtSync is not 14 bytes with OpCode 0x5200 low byte first [then] fail."""
    packet = encode_artsync()
    assert len(packet) == 14
    assert packet[8:10] == b"\x00\x52"
    assert packet[12:] == b"\x00\x00"
