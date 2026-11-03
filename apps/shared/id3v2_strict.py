"""Read-time checks on an ID3v2 tag, applied by a strict read so a malformed tag is an error, not an empty tag.

The checks depend on the frame model and the decoders in :mod:`apps.shared.id3v2`, which imports this module
at its foot. They run only when :func:`apps.shared.id3v2.read_tag` is called, so the cycle resolves.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

from apps.shared.id3v2 import (
    HEADER_SIZE,
    Frame,
    Id3Error,
    _check_text_encoding,
    _decode_apic,
    _decode_comm,
    _decode_geob,
    _decode_text_values,
    _decode_txxx,
    _syncsafe_to_int,
)


def _reject_cut_short_header(path: Path, header: bytes) -> None:
    """A file that starts with the ID3 signature but ends inside its header has a broken tag, not no tag."""
    if header[:3] == b"ID3" and len(header) < HEADER_SIZE:
        raise Id3Error(f"{path}: ID3 header is cut short at {len(header)} bytes")

def _check_footer_padding(path: Path, body: bytes, frames_end: int, has_footer: bool) -> None:
    """v2.4 forbids padding when a footer is present, so the frames must reach the end of the body."""
    if has_footer and frames_end != len(body):
        raise Id3Error(f"{path}: ID3v2.4 tag has a footer but {len(body) - frames_end} padding bytes follow the frames")

def _check_tag_header(path: Path, header: bytes, body: bytes, footer_bytes: bytes) -> None:
    """Reject a version, revision, size, footer or flag set the spec does not allow, so the tag is unreadable rather than empty."""
    major, revision, flags = header[3], header[4], header[5]
    if major not in (3, 4):
        raise Id3Error(f"{path}: ID3v2.{major} tag is not supported (only v2.3 / v2.4)")
    if revision == 0xFF:
        # The spec never uses 0xFF for the version or revision; a tag that says so is malformed, not empty.
        raise Id3Error(f"{path}: ID3v2 header revision byte is 0xFF, which the spec never uses")
    body_size = _syncsafe_to_int(header[6:10])
    if len(body) != body_size:
        raise Id3Error(f"{path}: ID3 tag claims {body_size} bytes but the file ends early")
    if major == 4 and flags & 0x10 and footer_bytes != b"3DI" + header[3:HEADER_SIZE]:
        # The footer copies the header after its 3DI magic. Without a matching one, the audio that
        # follows would be read as tag and lost on save.
        raise Id3Error(f"{path}: ID3v2.4 footer flag is set but no 10-byte 3DI footer matches the header")
    # v2.3 defines 0x80/0x40/0x20 and v2.4 defines 0x80/0x40/0x20/0x10; the low bits are reserved.
    reserved_flags = flags & (0x1F if major == 3 else 0x0F)
    if reserved_flags:
        raise Id3Error(f"{path}: ID3v2.{major} header sets reserved flag bits 0x{reserved_flags:02x}")

def _decode_text_frame(frame: Frame, major: int, errors: str) -> None:
    """Decode a text-bearing frame's text fields with the given error mode, raising on what that mode rejects."""
    if frame.frame_id == "TXXX":
        _decode_txxx(frame.data, major, errors)
    elif frame.frame_id.startswith("T"):
        _decode_text_values(frame.data, major, errors)
    elif frame.frame_id == "COMM":
        _decode_comm(frame.data, major, errors)
    elif frame.frame_id == "GEOB":
        _decode_geob(frame.data, major, errors)
    else:
        _decode_apic(frame.data, major, errors)

def _check_text_encodings_at_read(frames: list[Frame], major: int, path: Path) -> None:
    """Check the encoding byte of every text-bearing frame when the tag is read, not only when an accessor decodes it.

    A strict read may call only some accessors, so a bad frame that no accessor asks for would otherwise pass unseen.
    A text-bearing frame with no bytes has no encoding byte to read, so it is malformed too.
    """
    for frame in frames:
        if frame.opaque:
            continue
        if frame.frame_id == "POPM":
            _check_popm_body(frame.data, path)
            continue
        if frame.frame_id == "APIC":
            _check_apic_picture_type(frame.data, path)
        if not (frame.frame_id.startswith("T") or frame.frame_id in ("COMM", "GEOB", "APIC")):
            continue
        if not frame.data:
            raise Id3Error(f"{path}: text-bearing frame {frame.frame_id} has no encoding byte")
        _check_text_encoding(frame.data[0], major)
        try:
            _decode_text_frame(frame, major, "strict")
        except UnicodeDecodeError as exc:
            raise Id3Error(f"{path}: {frame.frame_id} text is not valid for its encoding") from exc

def _check_apic_picture_type(data: bytes, path: Path) -> None:
    """An APIC picture type is one of the values ID3v2 defines, 0x00 through 0x14; any other byte is malformed."""
    _mime, separator, rest = data[1:].partition(b"\x00")
    if separator and rest and rest[0] > 0x14:
        raise Id3Error(f"{path}: APIC picture type {rest[0]} is not defined by ID3v2")

def _check_popm_body(data: bytes, path: Path) -> None:
    """A POPM body is an email, a NUL, then a rating byte; a strict read rejects one that lacks either part."""
    _email, separator, rest = data.partition(b"\x00")
    if not separator or not rest:
        raise Id3Error(f"{path}: POPM frame has no email terminator or rating byte")

def _verify_extended_header(body: bytes, major: int, flags: int, offset: int, frames_end: int, unsynced: bool, path: Path) -> None:
    """Check the padding size and CRC an extended header declares, against the frames they cover."""
    if not flags & 0x40:
        return
    if major == 3:
        # The v2.3 padding size is the total padding: every byte after the frames, and no others.
        padding = struct.unpack(">I", body[6:10])[0]
        if padding != len(body) - frames_end:
            raise Id3Error(f"{path}: ID3 extended header declares {padding} bytes of padding but {len(body) - frames_end} follow the frames")
    expected = _extended_crc(body, major)
    # v2.3's CRC covers the frames only and is taken before unsynchronisation, which is already undone here. v2.4's
    # covers everything after the extended header, padding included; its order against unsynchronisation is not
    # pinned by the spec text, so it is checked only when the tag and its frames use none.
    if expected is not None and (major == 3 or not (flags & 0x80 or unsynced)):
        covered = body[offset:frames_end] if major == 3 else body[offset:]
        if zlib.crc32(covered) != expected:
            raise Id3Error(f"{path}: ID3 extended header CRC {expected:08x} does not match the tag data")

def _extended_crc(body: bytes, major: int) -> int | None:
    """The CRC-32 an extended header declares, or None when it declares none."""
    if major == 3:
        return struct.unpack(">I", body[10:14])[0] if body[4] & 0x80 else None
    if not body[5] & 0x20:
        return None
    pos = 7 if body[5] & 0x40 else 6  # the update flag's zero length byte comes first
    return _syncsafe_to_int(body[pos + 1 : pos + 6])

def _check_v23_extended_header(body: bytes, size: int) -> None:
    """Check the v2.3 field bytes after the size: how many there are, whether the CRC flag matches them, and the reserved bits."""
    # v2.3 counts the 6 or 10 field bytes after the size; the CRC flag needs the 10.
    if size < 6:
        raise Id3Error(f"ID3 extended header declares {size} field bytes, fewer than the 6 a v2.3 header needs")
    if size not in (6, 10):
        raise Id3Error(f"ID3 extended header declares {size} field bytes, but a v2.3 header has 6 or 10")
    if body[4] & 0x80 and size != 10:
        raise Id3Error("ID3 extended header sets the CRC flag but declares only 6 field bytes")
    if size == 10 and not body[4] & 0x80:
        raise Id3Error("ID3 extended header declares 10 field bytes but does not set the CRC flag")
    if body[4] & 0x7F or body[5]:
        raise Id3Error("ID3 extended header sets reserved flag bits; v2.3 defines only the CRC bit")

def _check_v24_extended_payloads(body: bytes, end: int) -> None:
    """Each flag set in a v2.4 extended header is followed by a length byte and that many payload bytes, in flag order."""
    reserved = body[5] & ~0x70  # v2.4 defines only update, CRC and restrictions; the rest must be clear
    if reserved:
        raise Id3Error(f"ID3 extended header sets reserved flag bits 0x{reserved:02x}")
    pos = 6
    for bit, length in ((0x40, 0), (0x20, 5), (0x10, 1)):  # update, CRC, restrictions
        if not body[5] & bit:
            continue
        if pos >= end or body[pos] != length or pos + 1 + length > end:
            raise Id3Error(f"ID3 extended header flag 0x{bit:02x} has no valid {length}-byte payload")
        if bit == 0x20:
            _check_crc_payload(body[pos + 1 : pos + 1 + length])
        pos += 1 + length
    if pos != end:
        raise Id3Error(f"ID3 extended header declares {end} bytes but its flags account for {pos}")

def _check_crc_payload(payload: bytes) -> None:
    """A v2.4 extended-header CRC is five syncsafe bytes that hold a 32-bit value."""
    if any(b & 0x80 for b in payload):
        raise Id3Error("ID3 extended header CRC payload is not syncsafe")
    if payload[0] > 0x0F:
        # Five syncsafe bytes hold 35 bits, but a CRC-32 is 32, so the first byte's top nibble must be zero.
        raise Id3Error("ID3 extended header CRC payload is wider than a 32-bit CRC")
