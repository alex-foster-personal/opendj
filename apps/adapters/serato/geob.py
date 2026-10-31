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
    text = encoded.split(b"\x00", 1)[0].replace(b"\n", b"").replace(b"\r", b"")
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
    return body[2:] if body.startswith(b"\x01\x01") else body


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
        return Markers2()
    body = _markers2_entries(geob_payload)
    if body is None:
        # Unrecognised version or undecodable base64 -- preserve as unknown
        # (single sub-tag named "__raw__" carrying the whole frame).
        return Markers2(unknown_tags=(("__raw__", bytes(geob_payload)),))

    cues: list[Markers2Cue] = []
    loops: list[Markers2Loop] = []
    track_color: int | None = None
    bpm_locked = False
    unknown: list[tuple[str, bytes]] = []

    pos = 0
    while pos < len(body):
        name, name_len = _read_null_terminated(body, pos)
        pos += name_len
        if not name:
            break  # the single NUL that ends the entry list
        if pos + 4 > len(body):
            break
        (length,) = struct.unpack_from(">I", body, pos)
        pos += 4
        if pos + length > len(body):
            break
        payload = body[pos : pos + length]
        pos += length

        if name == "CUE" and len(payload) >= 13:
            # reserved (1) + index (1) + position (4) + reserved (1) + RGB (3) + reserved (2) + UTF-8 name C-string
            idx = payload[1]
            (position,) = struct.unpack_from(">I", payload, 2)
            color = int.from_bytes(payload[7:10], "big")
            tail_name = payload[12:].split(b"\x00", 1)[0].decode("utf-8", "replace")
            cues.append(
                Markers2Cue(
                    index=idx,
                    position_ms=position,
                    color_rgb=color,
                    name=tail_name,
                    kind="hot",
                )
            )
        elif name == "LOOP" and len(payload) >= 21:
            idx = payload[1]
            (start,) = struct.unpack_from(">I", payload, 2)
            (end,) = struct.unpack_from(">I", payload, 6)
            color = int.from_bytes(payload[14:18], "big") & 0xFFFFFF
            locked = payload[19] == 0x01
            tail_name = payload[20:].split(b"\x00", 1)[0].decode("utf-8", "replace")
            loops.append(
                Markers2Loop(
                    index=idx,
                    start_ms=start,
                    end_ms=end,
                    color_rgb=color,
                    name=tail_name,
                    locked=locked,
                )
            )
        elif name == "COLOR" and len(payload) >= 4:
            track_color = int.from_bytes(payload[1:4], "big") & 0xFFFFFF
        elif name == "BPMLOCK" and len(payload) >= 1:
            bpm_locked = payload[0] == 0x01
        else:
            unknown.append((name, bytes(payload)))

    return Markers2(
        cues=tuple(cues),
        loops=tuple(loops),
        track_color_rgb=track_color,
        bpm_locked=bpm_locked,
        unknown_tags=tuple(unknown),
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


# ========================================================= mutagen I/O ===
#
# These helpers wire ``encode_markers2`` / ``encode_beatgrid`` onto real audio
# files through ``mutagen``. They are the plumbing layer the ``SeratoAdapter``
# calls from ``write()`` / ``read()`` so per-track cues + beatgrid actually
# land in (and are recovered from) the audio file's GEOB ID3 frames.
#
# Scope: MP3 (ID3v2) only for v1. FLAC/AIFF/WAV use different Serato tagging
# conventions (FLAC: a Vorbis ``SERATO_MARKERS_V2`` base64 comment; AIFF:
# APPL chunks). Those are out of scope here; the adapter downgrades to a
# structured warning for non-MP3 files so the write doesn't silently lose
# cue data.
#
# Frame layout comes from ``encode_markers2`` / ``encode_beatgrid`` (see
# above). Writing those bytes into an ID3 GEOB frame used mutagen, which
# this Apache-2.0 product does not depend on. ``write_geob_frames`` refuses
# without touching the file. ``read_geob_frames`` reads GEOB frames with the
# in-house ID3 parser, so a read does not need mutagen importable.


GEOB_OWNER: str = "DJ Pool"
"""Serato's GEOB owner string (a.k.a. ``encoding`` on the ID3 frame)."""

GEOB_MARKERS2_DESC: str = "Serato Markers2"
GEOB_BEATGRID_DESC: str = "Serato BeatGrid"
GEOB_MARKERS_LEGACY_DESC: str = "Serato Markers_"


def _is_mp3(path) -> bool:
    from pathlib import Path as _Path

    return _Path(path).suffix.lower() == ".mp3"


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


def read_geob_frames(audio_path) -> SeratoGEOB:
    """Read Serato GEOB frames from an MP3 with the in-house ID3 parser.

    A missing file, a non-MP3 path, or a file with no ID3 tag returns an
    empty bundle. This does not import mutagen.
    """
    from pathlib import Path as _Path

    from apps.shared.id3v2 import Id3Error, read_tag

    path = _Path(audio_path)
    if not path.is_file() or not _is_mp3(path):
        return SeratoGEOB()
    try:
        tag = read_tag(path)
    except (Id3Error, OSError):
        return SeratoGEOB()
    if tag is None:
        return SeratoGEOB()
    markers2 = Markers2()
    beatgrid = BeatGrid(markers=())
    opaque: dict[str, bytes] = {}
    legacy: MarkersLegacy | None = None
    for mime, _filename, desc, payload in tag.geob():
        del mime
        if desc == GEOB_MARKERS2_DESC:
            markers2 = parse_markers2(payload)
        elif desc == GEOB_BEATGRID_DESC:
            beatgrid = parse_beatgrid(payload)
        elif desc.startswith("Serato "):
            opaque[desc] = payload
            if desc == GEOB_MARKERS_LEGACY_DESC:
                legacy = parse_markers_legacy(payload)
    return SeratoGEOB(
        markers2=markers2, beatgrid=beatgrid, opaque_frames=opaque, markers_legacy=legacy
    )


def is_mp3_like_path(audio_path) -> bool:
    """Return True if ``audio_path`` has an ``.mp3`` suffix (v1 write scope)."""
    return _is_mp3(audio_path)
