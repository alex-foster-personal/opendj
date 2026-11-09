"""Serato GEOB ID3-frame codec -- per-file cues, loops, beatgrid.

Reads and writes the subset of Serato GEOB frames that carry per-track
metadata. Each frame is a base64-wrapped tag stream (for ``Serato Markers2``)
or a simple binary header + records (for ``Serato BeatGrid``).

Frames we parse:

  * ``Serato Markers2`` -- hot cues, loops, colour, flips. Base64 payload
    starts with a 0x01 version byte + 0x01 tag-marker byte, followed by a
    tag stream of ``CUE``, ``LOOP``, ``COLOR``, ``BPMLOCK`` sub-tags.
  * ``Serato BeatGrid`` -- non-terminal anchors + terminal marker. Binary,
    big-endian, no base64.
  * ``Serato Autotags`` -- gain + BPM + loudness, three doubles.

Frames we passthrough (bytes == bytes):

  * ``Serato Overview`` (waveform summary, out of scope for v0 open-dj).
  * ``Serato Analysis`` (analyser version flags; write as received).
  * ``Serato RelVol``, ``Serato Markers_`` (legacy).

The passthrough frames are stored on ``SeratoGEOB.extras`` so adapters
round-trip them intact per open-dj §9.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import struct
from dataclasses import dataclass, field
from typing import Literal

# Serato pads a Markers2 frame with NULs to at least this many bytes
# (serato-tags docs/serato_markers2.md; Mixxx markers2.cpp dumpID3).
_MARKERS2_MIN_SIZE = 470

# ============================================================= Markers2 ===


@dataclass(frozen=True)
class Markers2Cue:
    index: int
    position_ms: int
    color_rgb: int
    name: str

    kind: Literal["hot", "memory"] = "hot"


@dataclass(frozen=True)
class Markers2Loop:
    index: int
    start_ms: int
    end_ms: int
    color_rgb: int
    name: str
    locked: bool = False


@dataclass(frozen=True)
class Markers2:
    """Parsed ``Serato Markers2`` frame contents."""

    cues: tuple[Markers2Cue, ...] = ()
    loops: tuple[Markers2Loop, ...] = ()
    track_color_rgb: int | None = None
    bpm_locked: bool = False
    # Unknown sub-tags, preserved verbatim (name -> payload).
    unknown_tags: tuple[tuple[str, bytes], ...] = ()
    # True when an entry's declared length ran past the decoded body, so the
    # entries from there on could not be read. Not written back by the encoder.
    truncated: bool = False
    # Why the frame is incomplete, as the words after "Serato Markers2 frame "; empty when complete.
    truncation: str = ""
    # LOOP entries whose end does not come after their start. Such a loop has no length to import, so it is
    # counted as lost rather than kept as a zero-length loop; not written back by the encoder.
    backwards_loops: int = 0


def _read_null_terminated(buf: bytes, offset: int) -> tuple[str, int]:
    """Return (ascii-string, bytes-consumed-including-null) starting at offset."""
    end = buf.find(b"\x00", offset)
    if end < 0:
        return buf[offset:].decode("latin-1"), len(buf) - offset
    return buf[offset:end].decode("latin-1"), end - offset + 1


def _decode_markers2_base64(encoded: bytes) -> bytes:
    """Decode the base64 body of a ``Serato Markers2`` frame the way Serato
    writes it: NUL padding after the text, a linefeed every 72 characters, no
    ``=`` padding, and sometimes one character past a multiple of four, which
    serato-tags documents fixing by appending ``A``."""
    text, _, padding = encoded.partition(b"\x00")
    if padding.strip(b"\x00"):
        # Only NUL padding may follow the text; anything else after the first NUL is corruption, not padding.
        raise ValueError("Markers2 base64 text has non-NUL bytes after its NUL padding")
    text = text.replace(b"\n", b"").replace(b"\r", b"")
    text = text.rstrip(b"=")
    if len(text) % 4 == 1:
        text += b"A"
    text += b"=" * (-len(text) % 4)
    return base64.b64decode(text, validate=True)


def _markers2_entries(geob_payload: bytes) -> bytes | None:
    """The decoded entry list of a Markers2 frame, after both ``01 01``
    headers, or None when the frame is not one this codec can read."""
    if geob_payload[:2] != b"\x01\x01":
        return None
    try:
        body = _decode_markers2_base64(geob_payload[2:])
    except (ValueError, binascii.Error):
        return None
    if body[:2] == b"\x01\x01":
        return body[2:]
    # An empty body is an entry list cut off before it began, which the caller reports as truncated.
    # A non-empty body without its inner 01 01 header is not an entry list, so it is not read as one.
    return b"" if not body else None


def _truncation_reason(*, truncated: bool, ended: bool, body: bytes) -> str:
    """Why a decoded entry list is incomplete, or "" when it is complete. Even an empty entry list ends
    in its NUL; a body that runs out first is incomplete. Each kind loses a different thing to the reader."""
    if truncated:
        return "ends inside an entry"
    if ended:
        return ""
    return "has no entry list" if not body else "is missing its entry-list terminator"


class _Markers2Error(Exception):
    """A Markers2 frame that cannot be carried over intact, so it is kept whole as raw."""


def _markers2_name(raw: bytes) -> str:
    """The NUL-terminated UTF-8 name at the start of ``raw``. Raises when it has no terminator, is not UTF-8,
    or is followed by anything but NUL padding, since the name is the last field and those bytes would be lost."""
    name, terminator, rest = raw.partition(b"\x00")
    if not terminator or rest.strip(b"\x00"):
        raise _Markers2Error
    try:
        return name.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _Markers2Error from exc


def _require_entry_list_padding(body: bytes, pos: int) -> None:
    """Only NUL padding may follow the entry-list NUL; other bytes would hide entries from the reader."""
    if body[pos:].strip(b"\x00"):
        raise _Markers2Error


def _entry_payload(body: bytes, pos: int) -> tuple[bytes, int] | None:
    """The length-prefixed entry at ``pos``: its payload and the position after it, or None when it runs past ``body``."""
    if pos + 4 > len(body):
        return None
    (length,) = struct.unpack_from(">I", body, pos)
    end = pos + 4 + length
    if end > len(body):
        return None
    return body[pos + 4 : end], end


def _decode_markers2_loop(payload: bytes) -> Markers2Loop:
    """A LOOP entry's payload as a loop. An end at or before its start is returned as read, for the caller to count."""
    idx = payload[1]
    (start,) = struct.unpack_from(">I", payload, 2)
    (end,) = struct.unpack_from(">I", payload, 6)
    color = int.from_bytes(payload[14:18], "big") & 0xFFFFFF
    locked = payload[19] == 0x01
    return Markers2Loop(
        index=idx,
        start_ms=start,
        end_ms=end,
        color_rgb=color,
        name=_markers2_name(payload[20:]),
        locked=locked,
    )


def parse_markers2(geob_payload: bytes) -> Markers2:
    """Decode a ``Serato Markers2`` GEOB payload.

    Layout (serato-tags ``docs/serato_markers2.md``, checked against Mixxx's
    ``markers2.cpp`` and real Serato frames):

        0x01 0x01                -- frame header
        <base64-encoded body>    -- linefeed every 72 chars, then NUL padding

    The decoded body repeats the ``01 01`` header, then holds a sequence of
    ``\\0``-terminated name + uint32 length + payload entries, then one NUL.
    Known names: ``CUE``, ``LOOP``, ``COLOR``, ``BPMLOCK``.
    """
    if not geob_payload:
        # A frame with no bytes at all lacks even the 01 01 header a valid
        # empty frame carries, so whatever it held is lost.
        return Markers2(truncated=True, truncation="is empty")
    body = _markers2_entries(geob_payload)
    if body is None:
        # Unrecognised version or undecodable base64 -- preserve as unknown
        # (single sub-tag named "__raw__" carrying the whole frame).
        return Markers2(unknown_tags=(("__raw__", bytes(geob_payload)),))

    cues: list[Markers2Cue] = []
    loops: list[Markers2Loop] = []
    backwards_loops = 0
    track_color: int | None = None
    bpm_locked = False
    unknown: list[tuple[str, bytes]] = []

    truncated = False
    ended = False
    pos = 0
    try:
        while pos < len(body):
            name, name_len = _read_null_terminated(body, pos)
            pos += name_len
            if not name:
                _require_entry_list_padding(body, pos)
                ended = True
                break  # the single NUL that ends the entry list
            entry = _entry_payload(body, pos)
            if entry is None:
                truncated = True
                break
            payload, pos = entry

            if name == "CUE" and len(payload) >= 13:
                # reserved (1) + index (1) + position (4) + reserved (1) + RGB (3) + reserved (2) + UTF-8 name C-string
                idx = payload[1]
                (position,) = struct.unpack_from(">I", payload, 2)
                color = int.from_bytes(payload[7:10], "big")
                cues.append(
                    Markers2Cue(
                        index=idx,
                        position_ms=position,
                        color_rgb=color,
                        name=_markers2_name(payload[12:]),
                        kind="hot",
                    )
                )
            elif name == "LOOP" and len(payload) >= 21:
                loop = _decode_markers2_loop(payload)
                if loop.end_ms <= loop.start_ms:
                    backwards_loops += 1
                else:
                    loops.append(loop)
            elif name == "COLOR" and len(payload) >= 4:
                track_color = int.from_bytes(payload[1:4], "big") & 0xFFFFFF
            elif name == "BPMLOCK" and len(payload) >= 1:
                bpm_locked = payload[0] == 0x01
            else:
                unknown.append((name, bytes(payload)))
    except _Markers2Error:
        # A name without its NUL, a name that is not UTF-8, or bytes after the entry list cannot be carried over intact.
        return Markers2(unknown_tags=(("__raw__", bytes(geob_payload)),))

    truncation = _truncation_reason(truncated=truncated, ended=ended, body=body)
    return Markers2(
        cues=tuple(cues),
        loops=tuple(loops),
        track_color_rgb=track_color,
        bpm_locked=bpm_locked,
        unknown_tags=tuple(unknown),
        truncated=bool(truncation),
        truncation=truncation,
        backwards_loops=backwards_loops,
    )


def _encode_cue(cue: Markers2Cue) -> bytes:
    body = bytearray(12)
    body[0] = 0
    body[1] = cue.index & 0xFF
    struct.pack_into(">I", body, 2, cue.position_ms & 0xFFFFFFFF)
    body[6] = 0
    body[7:10] = (cue.color_rgb & 0xFFFFFF).to_bytes(3, "big")
    body[10:12] = b"\x00\x00"
    body += cue.name.encode("utf-8") + b"\x00"
    return bytes(body)


def _encode_loop(loop: Markers2Loop) -> bytes:
    body = bytearray(20)
    body[0] = 0
    body[1] = loop.index & 0xFF
    struct.pack_into(">I", body, 2, loop.start_ms & 0xFFFFFFFF)
    struct.pack_into(">I", body, 6, loop.end_ms & 0xFFFFFFFF)
    # bytes 10..13 -- reserved 0xFFFFFFFF per docs
    body[10:14] = b"\xFF\xFF\xFF\xFF"
    body[14:18] = (loop.color_rgb & 0xFFFFFFFF).to_bytes(4, "big")
    body[18] = 0
    body[19] = 0x01 if loop.locked else 0x00
    body += loop.name.encode("utf-8") + b"\x00"
    return bytes(body)


def encode_markers2(markers: Markers2) -> bytes:
    """Re-encode a ``Markers2`` into a GEOB payload."""
    # Special-case: Markers2 parsed from an unrecognised version is stored as
    # a single ``__raw__`` unknown_tag carrying the entire original frame. If
    # that is the ONLY content, round-trip verbatim to preserve the bytes
    # Serato gave us. If the caller has ALSO supplied cues / loops / color /
    # bpm_locked / other unknown tags, we cannot safely splice a full
    # ``__raw__`` frame into a tag stream; fall through to normal encoding
    # and silently drop the ``__raw__`` blob (it would otherwise corrupt the
    # stream and, worse, overwrite the structured cues/loops the caller set).
    has_structured_content = (
        bool(markers.cues)
        or bool(markers.loops)
        or markers.track_color_rgb is not None
        or markers.bpm_locked
        or any(name != "__raw__" for name, _ in markers.unknown_tags)
    )
    if not has_structured_content:
        for name, raw in markers.unknown_tags:
            if name == "__raw__":
                return raw

    body = bytearray()

    def _append(name: str, payload: bytes) -> None:
        body.extend(name.encode("ascii") + b"\x00")
        body.extend(struct.pack(">I", len(payload)))
        body.extend(payload)

    for cue in markers.cues:
        _append("CUE", _encode_cue(cue))
    for loop in markers.loops:
        _append("LOOP", _encode_loop(loop))
    if markers.track_color_rgb is not None:
        payload = b"\x00" + (markers.track_color_rgb & 0xFFFFFF).to_bytes(3, "big")
        _append("COLOR", payload)
    if markers.bpm_locked:
        _append("BPMLOCK", b"\x01")
    for name, raw in markers.unknown_tags:
        if name == "__raw__":
            # Cannot embed a verbatim full-frame blob inside the tag stream
            # without corrupting it; structured content wins. See the block
            # above for the pure-passthrough case.
            continue
        _append(name, raw)

    body.append(0)
    b64 = base64.b64encode(b"\x01\x01" + bytes(body)).rstrip(b"=")
    lines = b"\n".join(b64[i : i + 72] for i in range(0, len(b64), 72))
    return (b"\x01\x01" + lines).ljust(_MARKERS2_MIN_SIZE, b"\x00")


# ========================================================= Markers_ ===
# ``Serato Markers_`` (ID3 layout): serato-tags docs/serato_markers_.md and
# Mixxx src/track/serato/markers.cpp. Header 02 05 + uint32 entry count (14),
# then 14 entries of 22 bytes (5 cues, then 9 loops), then a 4-byte track
# color. Positions and colors are "serato32": 7 usable bits per byte.

_LEGACY_VERSION = b"\x02\x05"
_LEGACY_ENTRY_SIZE = 22
_LEGACY_ENTRY_COUNT = 14
_LEGACY_UNSET = 0x7F
_LEGACY_TYPE_CUE = 1
_LEGACY_TYPE_LOOP = 3
#: Markers_ always holds an entry for each of the first five hot cues.
LEGACY_HOT_CUE_SLOTS = 5
#: ...followed by an entry for each of the first nine saved loops.
LEGACY_LOOP_SLOTS = 9
_LEGACY_SET = 0x00
_LEGACY_UNSET_POSITION = b"\x7f\x7f\x7f\x7f"
_LEGACY_RESERVED = b"\x00\x7f\x7f\x7f\x7f\x7f"
_LEGACY_SIZE = 6 + _LEGACY_ENTRY_SIZE * _LEGACY_ENTRY_COUNT + 4


def _is_serato32(raw: bytes) -> bool:
    """True when 4 bytes are a valid serato32 encoding of a 24-bit value:
    7 usable bits per byte, and only the low 3 bits of the first byte."""
    return all(byte <= 0x7F for byte in raw) and raw[0] <= 0x07


def _serato32(raw: bytes) -> int:
    """Decode 4 serato32 bytes to the 24-bit value they carry."""
    w, x, y, z = raw
    c = (z & 0x7F) | ((y & 0x01) << 7)
    b = ((y & 0x7F) >> 1) | ((x & 0x03) << 6)
    a = ((x & 0x7F) >> 2) | ((w & 0x07) << 5)
    return (a << 16) | (b << 8) | c


@dataclass(frozen=True)
class MarkersLegacy:
    """What ``Serato Markers_`` says is set. Names live only in Markers2."""

    #: Hot cue index -> (position ms, RGB) for each cue Markers_ marks set.
    cues: dict[int, tuple[int, int]]
    #: Loop index -> (start ms, end ms, locked) for each loop marked set.
    loops: dict[int, tuple[int, int, bool]]


def _legacy_header_ok(geob_payload: bytes) -> bool:
    """The exact size, the 02 05 version, 14 entries, and a serato32 track color."""
    return (
        len(geob_payload) == _LEGACY_SIZE
        and geob_payload[:2] == _LEGACY_VERSION
        and int.from_bytes(geob_payload[2:6], "big") == _LEGACY_ENTRY_COUNT
        and _is_serato32(geob_payload[-4:])  # the track color
    )


def _legacy_fields_ok(entry: bytes, has_start: bool, has_end: bool) -> bool:
    """A set position is serato32 within 24 bits; an unset one is exactly
    the 7f 7f 7f 7f filler. Bytes 10-15 are the fixed 00 7f 7f 7f 7f 7f
    run every documented frame carries. The color is serato32 and the lock
    byte is 0 or 1, nothing else."""
    for has, raw in ((has_start, entry[1:5]), (has_end, entry[6:10])):
        if not (_is_serato32(raw) if has else raw == _LEGACY_UNSET_POSITION):
            return False
    return entry[10:16] == _LEGACY_RESERVED and _is_serato32(entry[16:20]) and entry[21] in (0x00, 0x01)


def _legacy_entry_ok(n: int, entry: bytes) -> bool:
    """Whether entry ``n`` is one Serato writes in that slot."""
    start_flag, end_flag, kind = entry[0], entry[5], entry[20]
    if start_flag not in (_LEGACY_SET, _LEGACY_UNSET) or end_flag not in (_LEGACY_SET, _LEGACY_UNSET):
        return False
    has_start = start_flag == _LEGACY_SET
    has_end = end_flag == _LEGACY_SET
    expected_kind = _LEGACY_TYPE_CUE if n < LEGACY_HOT_CUE_SLOTS else _LEGACY_TYPE_LOOP
    # An unset cue slot may carry type 0; a set entry must say what it is,
    # and an unset loop slot keeps loop type 3, as Serato and Mixxx write it.
    if kind != expected_kind and not (kind == 0 and not has_start and n < LEGACY_HOT_CUE_SLOTS):
        return False
    if not _legacy_fields_ok(entry, has_start, has_end):
        return False
    if n < LEGACY_HOT_CUE_SLOTS:
        # A cue is a single point: Serato leaves its end unset, and only a
        # loop can be locked. A set end or a set lock on a cue is not a
        # frame Serato writes.
        return not has_end and entry[21] == 0x00
    # A loop is set at both ends or neither; one end alone is not a frame
    # Serato writes, so it is not trusted to clear a loop.
    return has_start == has_end


def parse_markers_legacy(geob_payload: bytes) -> MarkersLegacy | None:
    """Decode an ID3 ``Serato Markers_`` payload, or None when it is not
    exactly the 14-entry 02 05 layout (another layout is never guessed at).

    The layout is checked whole before anything is returned: the exact size,
    five cue entries then nine loop entries in that order, and set flags that
    are either set or unset. A slot's number is its position, never a count
    of entries of one type, so a reordered frame cannot shift cues into
    other slots.
    """
    if not _legacy_header_ok(geob_payload):
        return None
    cues: dict[int, tuple[int, int]] = {}
    loops: dict[int, tuple[int, int, bool]] = {}
    for n in range(_LEGACY_ENTRY_COUNT):
        entry = geob_payload[6 + n * _LEGACY_ENTRY_SIZE : 6 + (n + 1) * _LEGACY_ENTRY_SIZE]
        if not _legacy_entry_ok(n, entry):
            return None
        if entry[0] != _LEGACY_SET:
            continue
        if n < LEGACY_HOT_CUE_SLOTS:
            position, color = _serato32(entry[1:5]), _serato32(entry[16:20])
            # A black cue at 0 ms is the known bogus entry (Mixxx issue
            # #11283 discards it the same way), so its slot reads as unset.
            if position or color:
                cues[n] = (position, color)
        else:
            start, end = _serato32(entry[1:5]), _serato32(entry[6:10])
            # A loop ending at 0 ms is a bogus legacy entry (Mixxx rejects it
            # the same way), so its slot reads as unset, as the cue branch does.
            if end:
                loops[n - LEGACY_HOT_CUE_SLOTS] = (start, end, entry[21] == 0x01)
    return MarkersLegacy(cues=cues, loops=loops)


def _overlay_loops(markers2: Markers2, legacy: MarkersLegacy) -> tuple[Markers2Loop, ...]:
    """Markers2's loops past the first nine, plus each loop Markers_ sets,
    keeping the Markers2 color and name for that slot."""
    old_loops = {loop.index: loop for loop in markers2.loops}
    loops_by_index = {index: loop for index, loop in old_loops.items() if index >= LEGACY_LOOP_SLOTS}
    for index, (start, end, locked) in legacy.loops.items():
        old = old_loops.get(index)
        loops_by_index[index] = Markers2Loop(
            index=index,
            start_ms=start,
            end_ms=end,
            color_rgb=old.color_rgb if old is not None else 0,
            name=old.name if old is not None else "",
            locked=locked,
        )
    return tuple(loops_by_index[index] for index in sorted(loops_by_index))


def overlay_markers_legacy(markers2: Markers2, legacy: MarkersLegacy | None) -> Markers2:
    """Apply ``Serato Markers_`` over ``Serato Markers2`` the way Serato does.

    Serato prefers Markers_ where both exist: a cue or loop set there takes
    its position (and a cue its color) from Markers_, keeping the Markers2
    name. A hot cue in the first five slots, or a loop in the first nine,
    that Markers_ leaves unset is not set in Serato, so it is dropped. Mixxx
    ``SeratoTags::getCueInfos`` does the same for cues. Only hot cues and
    loops are governed: any other cue kind passes through untouched. With no
    readable Markers_, Markers2 stands as it is.
    """
    if legacy is None:
        return markers2
    names = {cue.index: cue.name for cue in markers2.cues if cue.kind == "hot"}
    cues = [
        cue for cue in markers2.cues
        if cue.kind != "hot" or (cue.index >= LEGACY_HOT_CUE_SLOTS and cue.index not in legacy.cues)
    ]
    cues.extend(
        Markers2Cue(index=index, position_ms=position, color_rgb=color, name=names.get(index, ""), kind="hot")
        for index, (position, color) in legacy.cues.items()
    )
    return dataclasses.replace(
        markers2,
        cues=tuple(sorted(cues, key=lambda cue: cue.index)),
        loops=_overlay_loops(markers2, legacy),
    )


# ============================================================ BeatGrid ===


@dataclass(frozen=True)
class BeatGridMarker:
    position_seconds: float
    bpm: float | None = None          # None for non-terminal anchors
    beats_till_next: int | None = None # only for non-terminal anchors


@dataclass(frozen=True)
class BeatGrid:
    markers: tuple[BeatGridMarker, ...]
    footer: bytes = b""               # unknown trailing bytes preserved


def parse_beatgrid(payload: bytes) -> BeatGrid:
    """Decode a ``Serato BeatGrid`` GEOB payload.

    Layout:

        version  (uint16 big-endian)  -- typically 0x0001
        count    (uint32 big-endian)  -- number of markers
        markers  (count-1 non-terminal + 1 terminal)
        footer   (1 byte, often 0x00) -- we round-trip as-is

    Non-terminal marker = position (float32) + beats_till_next (uint32).
    Terminal marker     = position (float32) + bpm (float32).
    """
    if len(payload) < 6:
        return BeatGrid(markers=(), footer=bytes(payload))
    count = int.from_bytes(payload[2:6], "big")
    if count == 0:
        return BeatGrid(markers=(), footer=bytes(payload[6:]))
    markers: list[BeatGridMarker] = []
    pos = 6
    for i in range(count):
        is_terminal = i == count - 1
        if is_terminal:
            if pos + 8 > len(payload):
                break
            (position, bpm) = struct.unpack_from(">ff", payload, pos)
            pos += 8
            markers.append(BeatGridMarker(position_seconds=float(position), bpm=float(bpm)))
        else:
            if pos + 8 > len(payload):
                break
            (position, beats) = struct.unpack_from(">fI", payload, pos)
            pos += 8
            markers.append(
                BeatGridMarker(
                    position_seconds=float(position),
                    beats_till_next=int(beats),
                )
            )
    return BeatGrid(markers=tuple(markers), footer=bytes(payload[pos:]))


def encode_beatgrid(grid: BeatGrid) -> bytes:
    parts: list[bytes] = [b"\x00\x01", struct.pack(">I", len(grid.markers))]
    for i, m in enumerate(grid.markers):
        is_terminal = i == len(grid.markers) - 1
        if is_terminal:
            parts.append(struct.pack(">ff", m.position_seconds, m.bpm or 0.0))
        else:
            parts.append(struct.pack(">fI", m.position_seconds, m.beats_till_next or 4))
    parts.append(grid.footer)
    return b"".join(parts)


# ============================================================ combined ===


@dataclass(frozen=True)
class SeratoGEOB:
    """Bundle of all Serato GEOB frames for one file."""

    markers2: Markers2 = field(default_factory=Markers2)
    beatgrid: BeatGrid = field(default_factory=lambda: BeatGrid(markers=()))
    #: ``Serato Markers_`` as read (None when absent or not the ID3 layout).
    #: Its bytes also stay in ``opaque_frames``.
    markers_legacy: MarkersLegacy | None = None
    # Unparsed frames kept bytes-for-bytes. Keys are the GEOB description
    # (e.g. "Serato Overview"); values are the raw GEOB payload bytes.
    opaque_frames: dict[str, bytes] = field(default_factory=dict)
    # GEOB frames that are compressed or encrypted, so neither their
    # description nor their payload could be read. One may be Markers2.
    sealed_geob_frames: int = 0
    # Earlier Markers2 frames in a tag that repeats it. Only the last one is
    # ``markers2``, but a loss in any of them is still a loss to report.
    shadowed_markers2: tuple[Markers2, ...] = ()


# ========================================================= mutagen I/O ===
#
# These helpers wire ``encode_markers2`` / ``encode_beatgrid`` onto real audio
# files through ``mutagen``. They are the plumbing layer the ``SeratoAdapter``
# calls from ``write()`` / ``read()`` so per-track cues + beatgrid actually
# land in (and are recovered from) the audio file's GEOB ID3 frames.
#
# Reads cover MP3 (ID3 at the start), AIFF (an ``ID3 `` chunk), FLAC (Vorbis
# ``SERATO_MARKERS_V2`` / ``SERATO_BEATGRID`` comments) and MP4 (freeform
# ``com.serato.dj`` atoms). ``write_geob_frames`` still refuses for every
# format (mutagen was removed).
#
# Frame layout comes from ``encode_markers2`` / ``encode_beatgrid`` (see
# above). ``read_geob_frames`` uses the in-house ID3, FLAC and MP4 parsers.


class SeratoTagError(ValueError):
    """A FLAC or MP4 file whose Serato tag cannot be read; the message names the file."""


GEOB_OWNER: str = "DJ Pool"
"""Serato's GEOB owner string (a.k.a. ``encoding`` on the ID3 frame)."""

GEOB_MARKERS2_DESC: str = "Serato Markers2"
GEOB_BEATGRID_DESC: str = "Serato BeatGrid"
GEOB_MARKERS_LEGACY_DESC: str = "Serato Markers_"

FLAC_MARKERS2_KEY = "SERATO_MARKERS_V2"
FLAC_BEATGRID_KEY = "SERATO_BEATGRID"
MP4_SERATO_MEAN = "com.serato.dj"
MP4_MARKERS2_NAME = "markersv2"
MP4_BEATGRID_NAME = "beatgrid"

_MP3_SUFFIXES = frozenset({".mp3"})
_AIFF_SUFFIXES = frozenset({".aif", ".aiff"})
_FLAC_SUFFIXES = frozenset({".flac"})
_MP4_SUFFIXES = frozenset({".m4a", ".mp4"})
_SERATO_READ_SUFFIXES = _MP3_SUFFIXES | _AIFF_SUFFIXES | _FLAC_SUFFIXES | _MP4_SUFFIXES

_GEOB_TEXT_PREFIX = b"application/octet-stream\x00"


def _is_mp3(path) -> bool:
    from pathlib import Path as _Path

    return _Path(path).suffix.lower() in _MP3_SUFFIXES


def write_geob_frames(
    audio_path,
    *,
    markers2: Markers2 | None = None,
    beatgrid: BeatGrid | None = None,
    opaque: dict[str, bytes] | None = None,
) -> None:
    """Refuse to write Serato GEOB frames. Does not modify ``audio_path``.

    Encoding of Markers2 and BeatGrid payloads remains available as pure
    Python (:func:`encode_markers2`, :func:`encode_beatgrid`). Putting those
    bytes into an ID3 GEOB frame required mutagen (GPL-2.0-or-later).
    """
    del markers2, beatgrid, opaque
    from apps.shared.tag_writer import TagWriteRemoved

    raise TagWriteRemoved(
        "writing Serato GEOB frames into audio files was removed because "
        f"mutagen is GPL-2.0-or-later (refusing {audio_path})"
    )


def read_geob_frames(audio_path, *, strict: bool = True) -> SeratoGEOB:
    """Read Serato Markers2 and BeatGrid from MP3, AIFF, FLAC or MP4.

    A missing file or an unsupported suffix returns an empty bundle. A tag that
    is present but cannot be read raises :class:`Id3Error`, :class:`SeratoTagError`
    or ``OSError`` by default; only ``strict=False`` yields an empty bundle.
    """
    from pathlib import Path as _Path

    from apps.shared.id3v2 import Id3Error

    path = _Path(audio_path)
    if not path.is_file():
        return SeratoGEOB()
    suffix = path.suffix.lower()
    if suffix not in _SERATO_READ_SUFFIXES:
        return SeratoGEOB()
    try:
        if suffix in _MP3_SUFFIXES:
            return _read_id3_geob(path)
        if suffix in _AIFF_SUFFIXES:
            return _read_aiff_geob(path)
        if suffix in _FLAC_SUFFIXES:
            return _read_flac(path)
        return _read_mp4(path)
    except (Id3Error, SeratoTagError, OSError):
        if strict:
            raise
        return SeratoGEOB()


def _read_id3_geob(path) -> SeratoGEOB:
    from apps.shared.id3v2 import read_tag

    tag = read_tag(path)
    if tag is None:
        return SeratoGEOB()
    _require_tag_frames(tag, path)
    return _bundle_from_tag(tag)


def _read_aiff_geob(path) -> SeratoGEOB:
    from apps.shared.id3v2 import read_aiff_tag

    tag = read_aiff_tag(path)
    if tag is None:
        return SeratoGEOB()
    _require_tag_frames(tag, path)
    return _bundle_from_tag(tag)


def _decode_text_tag(raw: bytes | str, desc: str, path, *, key: str = "") -> bytes:
    import base64
    import binascii
    import re

    if isinstance(raw, str):
        try:
            raw = raw.encode("ascii")
        except UnicodeEncodeError as exc:
            raise SeratoTagError(f"{path.name}: Serato tag is not ASCII") from exc
    text = re.sub(rb"[\r\n\t ]+", b"", raw).rstrip(b"\x00")
    text = text.rstrip(b"=")
    if len(text) % 4 == 1:
        label = f"{key} " if key else ""
        raise SeratoTagError(f"{path.name}: {label}is not valid base64")
    padded = text + b"=" * ((4 - len(text) % 4) % 4)
    try:
        decoded = base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError) as exc:
        label = f"{key} " if key else ""
        raise SeratoTagError(f"{path.name}: {label}is not valid base64") from exc
    if not decoded.startswith(_GEOB_TEXT_PREFIX):
        raise SeratoTagError(f"{path.name}: Serato tag missing GEOB header")
    rest = decoded[len(_GEOB_TEXT_PREFIX) :]
    null = rest.find(b"\x00")
    if null < 0:
        raise SeratoTagError(f"{path.name}: Serato tag missing filename terminator")
    rest = rest[null + 1 :]
    null = rest.find(b"\x00")
    if null < 0:
        raise SeratoTagError(f"{path.name}: Serato tag missing description terminator")
    if rest[:null].decode("ascii", "replace") != desc:
        raise SeratoTagError(f"{path.name}: Serato tag description mismatch (expected {desc!r})")
    return rest[null + 1 :]


def _bundle_from_payloads(markers2: bytes | None, beatgrid: bytes | None) -> SeratoGEOB:
    return SeratoGEOB(
        markers2=parse_markers2(markers2) if markers2 is not None else Markers2(),
        beatgrid=parse_beatgrid(beatgrid) if beatgrid is not None else BeatGrid(markers=()),
    )


def _read_flac(path) -> SeratoGEOB:
    from apps.shared import flac_meta
    from apps.shared.vorbis_comment import VorbisCommentError

    try:
        meta = flac_meta.read(path)
    except (flac_meta.FlacError, VorbisCommentError) as exc:
        raise SeratoTagError(f"{path.name}: FLAC metadata could not be read") from exc
    markers_raw = meta.first(FLAC_MARKERS2_KEY)
    beatgrid_raw = meta.first(FLAC_BEATGRID_KEY)
    if markers_raw is None and beatgrid_raw is None:
        return SeratoGEOB()
    markers2 = (
        _decode_text_tag(markers_raw, GEOB_MARKERS2_DESC, path, key=FLAC_MARKERS2_KEY)
        if markers_raw is not None
        else None
    )
    beatgrid = (
        _decode_text_tag(beatgrid_raw, GEOB_BEATGRID_DESC, path, key=FLAC_BEATGRID_KEY)
        if beatgrid_raw is not None
        else None
    )
    return _bundle_from_payloads(markers2, beatgrid)


def _read_mp4(path) -> SeratoGEOB:
    from apps.shared import mp4_meta

    try:
        meta = mp4_meta.read(path)
    except mp4_meta.Mp4Error as exc:
        raise SeratoTagError(f"{path.name}: MP4 metadata could not be read") from exc
    markers_raw = meta.freeform_bytes(MP4_MARKERS2_NAME, MP4_SERATO_MEAN)
    beatgrid_raw = meta.freeform_bytes(MP4_BEATGRID_NAME, MP4_SERATO_MEAN)
    if markers_raw is None and beatgrid_raw is None:
        return SeratoGEOB()
    markers2 = (
        _decode_text_tag(markers_raw, GEOB_MARKERS2_DESC, path, key=MP4_MARKERS2_NAME)
        if markers_raw is not None
        else None
    )
    beatgrid = (
        _decode_text_tag(beatgrid_raw, GEOB_BEATGRID_DESC, path, key=MP4_BEATGRID_NAME)
        if beatgrid_raw is not None
        else None
    )
    return _bundle_from_payloads(markers2, beatgrid)


def _require_tag_frames(tag, path) -> None:
    """An ID3 tag with no frames is malformed under the spec, not a clean tag with no Serato data."""
    from apps.shared.id3v2 import Id3Error

    if not tag.frames:
        raise Id3Error(f"{path}: ID3 tag holds no frames, which the spec does not allow")


def _bundle_from_tag(tag) -> SeratoGEOB:
    """The Serato frames of a read tag, plus a count of GEOB frames sealed
    by compression or encryption, which are never decoded."""
    markers2s: list[Markers2] = []
    beatgrid = BeatGrid(markers=())
    opaque: dict[str, bytes] = {}
    legacy: MarkersLegacy | None = None
    for mime, _filename, desc, payload in tag.geob():
        del mime
        if desc == GEOB_MARKERS2_DESC:
            markers2s.append(parse_markers2(payload))
        elif desc == GEOB_BEATGRID_DESC:
            beatgrid = parse_beatgrid(payload)
        elif desc.startswith("Serato "):
            opaque[desc] = payload
            if desc == GEOB_MARKERS_LEGACY_DESC:
                legacy = parse_markers_legacy(payload)
    sealed = sum(1 for frame in tag.frames if frame.frame_id == "GEOB" and frame.opaque)
    return SeratoGEOB(
        markers2=markers2s[-1] if markers2s else Markers2(),
        beatgrid=beatgrid,
        opaque_frames=opaque,
        markers_legacy=legacy,
        sealed_geob_frames=sealed,
        shadowed_markers2=tuple(markers2s[:-1]),
    )


def is_mp3_like_path(audio_path) -> bool:
    """Return True if ``audio_path`` has an ``.mp3`` suffix (v1 write scope)."""
    return _is_mp3(audio_path)


def is_serato_readable_path(audio_path) -> bool:
    """Return True if ``read_geob_frames`` can read Serato tags from this path."""
    from pathlib import Path as _Path

    return _Path(audio_path).suffix.lower() in _SERATO_READ_SUFFIXES
