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
import struct
from dataclasses import dataclass, field
from typing import Literal

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


def parse_markers2(geob_payload: bytes) -> Markers2:
    """Decode a ``Serato Markers2`` GEOB payload.

    Layout (from public docs):

        0x01                     -- version byte
        0x01                     -- sub-version
        <base64-encoded body>    -- the remainder, possibly padded

    The decoded body is a sequence of ``\\0``-terminated name + uint32 length
    + payload. Known names: ``CUE``, ``LOOP``, ``COLOR``, ``BPMLOCK``.
    """
    if not geob_payload:
        return Markers2()
    if geob_payload[0] != 0x01 or geob_payload[1:2] != b"\x01":
        # Unrecognised version -- preserve as unknown (single empty sub-tag
        # with name "" and the raw bytes as payload).
        return Markers2(unknown_tags=(("__raw__", bytes(geob_payload)),))
    # Strip newlines inserted by Serato every 72 chars in the base64 body.
    b64_body = geob_payload[2:].replace(b"\n", b"").replace(b"\r", b"")
    try:
        body = base64.b64decode(b64_body, validate=False)
    except Exception:
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
        if pos + 4 > len(body):
            break
        (length,) = struct.unpack_from(">I", body, pos)
        pos += 4
        if pos + length > len(body):
            break
        payload = body[pos : pos + length]
        pos += length

        if name == "CUE" and len(payload) >= 13:
            # reserved (1) + index (1) + position (4) + reserved (1) + color (4) + reserved (2) + name C-string
            idx = payload[1]
            (position,) = struct.unpack_from(">I", payload, 2)
            color = int.from_bytes(payload[7:11], "big") & 0xFFFFFF
            tail_name = payload[13:].split(b"\x00", 1)[0].decode("latin-1", "replace")
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
            tail_name = payload[21:].split(b"\x00", 1)[0].decode("latin-1", "replace")
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
    body = bytearray(13)
    body[0] = 0
    body[1] = cue.index & 0xFF
    struct.pack_into(">I", body, 2, cue.position_ms & 0xFFFFFFFF)
    body[6] = 0
    body[7:11] = (cue.color_rgb & 0xFFFFFFFF).to_bytes(4, "big")
    body[11:13] = b"\x00\x00"
    body += cue.name.encode("latin-1", "replace") + b"\x00"
    return bytes(body)


def _encode_loop(loop: Markers2Loop) -> bytes:
    body = bytearray(21)
    body[0] = 0
    body[1] = loop.index & 0xFF
    struct.pack_into(">I", body, 2, loop.start_ms & 0xFFFFFFFF)
    struct.pack_into(">I", body, 6, loop.end_ms & 0xFFFFFFFF)
    # bytes 10..13 -- reserved 0xFFFFFFFF per docs
    body[10:14] = b"\xFF\xFF\xFF\xFF"
    body[14:18] = (loop.color_rgb & 0xFFFFFFFF).to_bytes(4, "big")
    body[18] = 0
    body[19] = 0x01 if loop.locked else 0x00
    body[20] = 0
    body += loop.name.encode("latin-1", "replace") + b"\x00"
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

    b64 = base64.b64encode(bytes(body))
    return b"\x01\x01" + b64


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
    for mime, _filename, desc, payload in tag.geob():
        del mime
        if desc == GEOB_MARKERS2_DESC:
            markers2 = parse_markers2(payload)
        elif desc == GEOB_BEATGRID_DESC:
            beatgrid = parse_beatgrid(payload)
        elif desc.startswith("Serato "):
            opaque[desc] = payload
    return SeratoGEOB(markers2=markers2, beatgrid=beatgrid, opaque_frames=opaque)


def is_mp3_like_path(audio_path) -> bool:
    """Return True if ``audio_path`` has an ``.mp3`` suffix (v1 write scope)."""
    return _is_mp3(audio_path)
