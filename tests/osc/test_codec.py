"""OSC 1.0 codec byte-level round trips and strict decoding (AGENT-12237).

[if] OSC messages or bundles are encoded then decoded [then] they round-trip exactly, [else stop].
"""

from __future__ import annotations

import struct

import pytest

from apps.osc.codec import (
    OscBundle,
    OscError,
    OscMessage,
    decode_packet,
    encode_message,
    encode_packet,
)

pytestmark = pytest.mark.requirement("AGENT-12237")

# Hand-built from the OSC 1.0 spec layout (synthesized fixture).
EXPECTED_MESSAGE_BYTES: bytes = (
    b"/deck/play\x00\x00" + b",ifs\x00\x00\x00\x00" + struct.pack(">i", -7) + struct.pack(">f", 0.5) + b"A\x00\x00\x00"
)


def test_message_round_trips_int_float_string() -> None:
    """[if] a message with int, float and string args round-trips [then] address and values match, [else stop]."""
    message = OscMessage("/deck/play", (-7, 0.5, "A"))
    data = encode_message(message)
    assert data == EXPECTED_MESSAGE_BYTES
    assert decode_packet(data) == message


def test_nested_bundle_keeps_order_and_timetag() -> None:
    """[if] a nested bundle is parsed [then] element order and timetags are kept, [else stop]."""
    inner = OscBundle(2**40 + 5, (OscMessage("/b", (2,)), OscMessage("/c", ("x",))))
    bundle = OscBundle(2**63 + 9, (OscMessage("/a", (1.25,)), inner, OscMessage("/d")))
    data = encode_packet(bundle)
    assert data[:16] == b"#bundle\x00" + struct.pack(">Q", 2**63 + 9)
    assert decode_packet(data) == bundle
    assert encode_packet(decode_packet(data)) == data


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(EXPECTED_MESSAGE_BYTES[:-1], id="length-not-multiple-of-4"),
        pytest.param(EXPECTED_MESSAGE_BYTES[:-4], id="missing-string-arg"),
        pytest.param(b"/deck/play\x00X" + EXPECTED_MESSAGE_BYTES[12:], id="non-zero-padding"),
        pytest.param(b"/a\x00\x00,x\x00\x00", id="unknown-type-tag"),
        pytest.param(b"/a\x00\x00ifs\x00", id="type-tags-without-comma"),
        pytest.param(b"/a\x00\x00,\x00\x00\x00\x00\x00\x00\x01", id="trailing-bytes"),
        pytest.param(b"#bundle\x00" + b"\x00" * 7 + b"\x01" + struct.pack(">i", 64) + b"/a\x00\x00", id="bad-size"),
        pytest.param(b"#bundle\x00\x00\x00", id="truncated-timetag"),
    ],
)
def test_malformed_packets_raise(data: bytes) -> None:
    """[if] padding, type tags or lengths are malformed [then] decoding raises, no partial message, [else stop]."""
    with pytest.raises(OscError):
        decode_packet(data)


@pytest.mark.parametrize("arg", [True, b"blob", None, 2**31, [1]])
def test_unsupported_argument_types_raise(arg: object) -> None:
    """[if] an unsupported argument type is encoded [then] the encoder raises, never substitutes, [else stop]."""
    with pytest.raises(OscError):
        encode_message(OscMessage("/a", (arg,)))  # type: ignore[arg-type]
