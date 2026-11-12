"""Dependency-free OSC 1.0 codec: messages and nested bundles with int, float and string arguments.

Reimplemented from the public OSC 1.0 specification. Every OSC string is NUL-terminated and
zero-padded to a multiple of four bytes; ``i`` is a big-endian int32, ``f`` a big-endian
float32 and ``s`` an OSC string. A bundle is ``#bundle``, a 64-bit NTP timetag, then
elements, each prefixed with its int32 byte size. Decoding is strict: any malformed
padding, type tag or length raises ``OscError`` instead of returning a partial packet.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

OscArg = int | float | str
BUNDLE_TAG: bytes = b"#bundle\x00"
TIMETAG_IMMEDIATE: int = 1
INT32_MIN: int = -(2**31)
INT32_MAX: int = 2**31 - 1
UINT64_MAX: int = 2**64 - 1


class OscError(ValueError):
    """An OSC packet is malformed or uses an unsupported argument type."""


@dataclass(frozen=True)
class OscMessage:
    """One OSC message: an address pattern and its typed arguments."""

    address: str
    args: tuple[OscArg, ...] = ()


@dataclass(frozen=True)
class OscBundle:
    """An OSC bundle: an NTP timetag and ordered message or bundle elements."""

    timetag: int
    elements: tuple[OscMessage | OscBundle, ...] = ()


OscPacket = OscMessage | OscBundle


# -----------------------------------------------------------------------------
# Encoding


def _pad4(data: bytes) -> bytes:
    return data + b"\x00" * (-len(data) % 4)


def _encode_string(text: str) -> bytes:
    raw = text.encode("utf-8")
    if b"\x00" in raw:
        raise OscError(f"OSC string must not contain NUL: {text!r}")
    return _pad4(raw + b"\x00")


def _encode_arg(arg: object) -> tuple[str, bytes]:
    if isinstance(arg, bool):
        raise OscError("unsupported OSC argument type: bool")
    elif isinstance(arg, int):
        if not INT32_MIN <= arg <= INT32_MAX:
            raise OscError(f"OSC int out of int32 range: {arg}")
        return "i", struct.pack(">i", arg)
    elif isinstance(arg, float):
        return "f", struct.pack(">f", arg)
    elif isinstance(arg, str):
        return "s", _encode_string(arg)
    raise OscError(f"unsupported OSC argument type: {type(arg).__name__}")


def encode_message(message: OscMessage) -> bytes:
    """Return the OSC 1.0 bytes for ``message``."""
    if not message.address.startswith("/"):
        raise OscError(f"OSC address must start with '/': {message.address!r}")
    encoded = [_encode_arg(arg) for arg in message.args]
    type_tags = "," + "".join(tag for tag, _ in encoded)
    return _encode_string(message.address) + _encode_string(type_tags) + b"".join(data for _, data in encoded)


def encode_bundle(bundle: OscBundle) -> bytes:
    """Return the OSC 1.0 bytes for ``bundle`` and its nested elements, in order."""
    if not 0 <= bundle.timetag <= UINT64_MAX:
        raise OscError(f"OSC timetag out of uint64 range: {bundle.timetag}")
    parts = [BUNDLE_TAG, struct.pack(">Q", bundle.timetag)]
    for element in bundle.elements:
        data = encode_packet(element)
        parts.append(struct.pack(">i", len(data)) + data)
    return b"".join(parts)


def encode_packet(packet: OscPacket) -> bytes:
    """Return the OSC bytes for a message or bundle."""
    if isinstance(packet, OscMessage):
        return encode_message(packet)
    elif isinstance(packet, OscBundle):
        return encode_bundle(packet)
    raise OscError(f"not an OSC packet: {type(packet).__name__}")


# -----------------------------------------------------------------------------
# Decoding


def _read_string(data: bytes, offset: int) -> tuple[str, int]:
    end = data.find(b"\x00", offset)
    if end < 0:
        raise OscError(f"unterminated OSC string at byte {offset}")
    padded_end = end + 1 + (-(end + 1 - offset) % 4)
    if padded_end > len(data):
        raise OscError(f"OSC string at byte {offset} is missing its padding")
    if any(data[end:padded_end]):
        raise OscError(f"OSC string at byte {offset} has non-zero padding")
    try:
        return data[offset:end].decode("utf-8"), padded_end
    except UnicodeDecodeError as error:
        raise OscError(f"OSC string at byte {offset} is not valid UTF-8") from error


def _read_fixed(data: bytes, offset: int, fmt: str) -> tuple[int | float, int]:
    size = struct.calcsize(fmt)
    if offset + size > len(data):
        raise OscError(f"OSC data truncated at byte {offset}")
    return struct.unpack_from(fmt, data, offset)[0], offset + size


def decode_message(data: bytes) -> OscMessage:
    """Return the message in ``data``; raise ``OscError`` on any malformed byte."""
    if len(data) % 4:
        raise OscError(f"OSC message length {len(data)} is not a multiple of 4")
    address, offset = _read_string(data, 0)
    if not address.startswith("/"):
        raise OscError(f"OSC address must start with '/': {address!r}")
    type_tags, offset = _read_string(data, offset)
    if not type_tags.startswith(","):
        raise OscError(f"OSC type tag string must start with ',': {type_tags!r}")
    args: list[OscArg] = []
    for tag in type_tags[1:]:
        if tag == "i":
            value, offset = _read_fixed(data, offset, ">i")
            args.append(int(value))
        elif tag == "f":
            value, offset = _read_fixed(data, offset, ">f")
            args.append(float(value))
        elif tag == "s":
            text, offset = _read_string(data, offset)
            args.append(text)
        else:
            raise OscError(f"unsupported OSC type tag: {tag!r}")
    if offset != len(data):
        raise OscError(f"{len(data) - offset} trailing bytes after OSC message")
    return OscMessage(address=address, args=tuple(args))


def decode_bundle(data: bytes) -> OscBundle:
    """Return the bundle in ``data`` with nested elements in order; raise on malformed bytes."""
    if not data.startswith(BUNDLE_TAG):
        raise OscError("OSC bundle must start with '#bundle'")
    timetag, offset = _read_fixed(data, len(BUNDLE_TAG), ">Q")
    elements: list[OscPacket] = []
    while offset < len(data):
        size, offset = _read_fixed(data, offset, ">i")
        size = int(size)
        if size <= 0 or size % 4 or offset + size > len(data):
            raise OscError(f"invalid OSC bundle element size {size} at byte {offset - 4}")
        elements.append(decode_packet(data[offset : offset + size]))
        offset += size
    return OscBundle(timetag=int(timetag), elements=tuple(elements))


def decode_packet(data: bytes) -> OscPacket:
    """Return the message or bundle in ``data``."""
    if data.startswith(b"#"):
        return decode_bundle(data)
    return decode_message(data)
