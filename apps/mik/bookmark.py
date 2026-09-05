"""macOS bookmark-blob path parser for MIK's ``ZSONG.ZBOOKMARKDATA``.

Why a real parser and not ``strings``: OBSERVED Tue 28 Jul 2026, an ASCII
scrape of these blobs truncates at the first non-ASCII byte and produced a
FALSE ZERO-match result on the first audit pass (MIK-AUDIT.md section 3).
The path is not stored as one string either -- it is an array of path
components, so a scrape cannot reassemble it correctly even when it does not
truncate.

Format (Apple ``alias``/bookmark, reverse-engineered; same layout ``mac_alias``
parses):

* header, 48 bytes: magic ``book`` | total size u32 | version u32 | header
  size u32 | 32 reserved bytes.
* at ``header_size``: u32 offset of the first table of contents, RELATIVE to
  ``header_size``.
* TOC: size u32 | magic u32 | id u32 | next-TOC offset u32 | entry count u32,
  then ``count`` entries of (key u32, value offset u32, unused u32). Value
  offsets are also relative to ``header_size``. A key with the high bit set is
  an offset to a string key, not an integer key -- we skip those, none of the
  keys we need are string-keyed.
* value record: length u32 | type u32 | ``length`` bytes of payload.

Types we care about: ``0x0101``/``0x0100`` UTF-8 string, ``0x0601`` array of
value offsets.

Keys we care about: ``0x1004`` path components (array of strings), ``0x2002``
volume path (string, ``/`` for the boot volume).

Everything here is defensive by construction and raises
:class:`BookmarkParseError` rather than guessing: a wrong path silently
mismatches a track, which is exactly the failure mode this parser exists to
avoid.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

MAGIC = b"book"

KEY_PATH_COMPONENTS = 0x1004
KEY_VOLUME_PATH = 0x2002
KEY_VOLUME_NAME = 0x2010

TYPE_STRING = 0x0101
TYPE_STRING_ALT = 0x0100
TYPE_ARRAY = 0x0601

_HEADER_MIN = 16
_TOC_HEADER_LEN = 20
_TOC_ENTRY_LEN = 12
_STRING_KEY_FLAG = 0x80000000
_MAX_TOCS = 32  # loop guard; real blobs have 1


class BookmarkParseError(ValueError):
    """The blob is not a parseable bookmark. Never fall back to a scrape."""


@dataclass(frozen=True)
class BookmarkPath:
    """A decoded bookmark: the absolute path plus what it was built from."""

    path: str
    volume_path: str
    volume_name: str | None
    components: tuple[str, ...]


def _u32(blob: bytes, offset: int) -> int:
    if offset < 0 or offset + 4 > len(blob):
        raise BookmarkParseError(
            f"u32 read at {offset} out of range (blob is {len(blob)} bytes)"
        )
    return struct.unpack_from("<I", blob, offset)[0]


def _read_value(blob: bytes, header_size: int, offset: int, depth: int = 0):
    if depth > 4:
        raise BookmarkParseError("bookmark value nesting deeper than 4")
    base = header_size + offset
    if base + 8 > len(blob):
        raise BookmarkParseError(
            f"value record at {base} runs past end of blob ({len(blob)} bytes)"
        )
    length, type_code = struct.unpack_from("<II", blob, base)
    start = base + 8
    end = start + length
    if end > len(blob):
        raise BookmarkParseError(
            f"value record at {base} claims {length} bytes, only "
            f"{len(blob) - start} remain"
        )
    payload = blob[start:end]
    if type_code in (TYPE_STRING, TYPE_STRING_ALT):
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BookmarkParseError(
                f"string record at {base} is not valid UTF-8: {exc}"
            ) from exc
    if type_code == TYPE_ARRAY:
        if length % 4:
            raise BookmarkParseError(
                f"array record at {base} has length {length}, not a multiple of 4"
            )
        return [
            _read_value(blob, header_size, _u32(payload, i * 4), depth + 1)
            for i in range(length // 4)
        ]
    raise BookmarkParseError(
        f"value record at {base} has unsupported type {type_code:#06x}"
    )


def _toc_entries(blob: bytes, header_size: int) -> dict[int, int]:
    entries: dict[int, int] = {}
    toc_offset = _u32(blob, header_size)
    seen: set[int] = set()
    while toc_offset:
        if toc_offset in seen or len(seen) >= _MAX_TOCS:
            raise BookmarkParseError("bookmark TOC chain loops or is too long")
        seen.add(toc_offset)
        base = header_size + toc_offset
        if base + _TOC_HEADER_LEN > len(blob):
            raise BookmarkParseError(
                f"TOC at {base} runs past end of blob ({len(blob)} bytes)"
            )
        _size, _magic, _ident, next_toc, count = struct.unpack_from("<5I", blob, base)
        span = base + _TOC_HEADER_LEN + count * _TOC_ENTRY_LEN
        if span > len(blob):
            raise BookmarkParseError(
                f"TOC at {base} declares {count} entries, which runs past the blob"
            )
        for index in range(count):
            entry = base + _TOC_HEADER_LEN + index * _TOC_ENTRY_LEN
            key, value_offset, _unused = struct.unpack_from("<3I", blob, entry)
            if key & _STRING_KEY_FLAG:
                continue  # string-keyed extension, none of ours
            entries[key] = value_offset
        toc_offset = next_toc
    return entries


def _validate_header(blob: bytes) -> int:
    """Check the 48-byte header and return ``header_size``."""
    if len(blob) < 48:
        raise BookmarkParseError(f"blob too short to be a bookmark ({len(blob)} bytes)")
    magic, total_size, _version, header_size = struct.unpack_from("<4sIII", blob, 0)
    if magic != MAGIC:
        raise BookmarkParseError(f"bad magic {magic!r}, expected {MAGIC!r}")
    if header_size < _HEADER_MIN or header_size > len(blob):
        raise BookmarkParseError(f"implausible header size {header_size}")
    if total_size != len(blob):
        raise BookmarkParseError(
            f"declared size {total_size} != actual {len(blob)} (truncated blob)"
        )
    return header_size


def _extract_components(
    blob: bytes, header_size: int, entries: dict[int, int]
) -> list[str]:
    if KEY_PATH_COMPONENTS not in entries:
        raise BookmarkParseError(
            f"no path-components record ({KEY_PATH_COMPONENTS:#06x}) in bookmark; "
            f"keys present: {sorted(hex(k) for k in entries)}"
        )
    raw_components = _read_value(blob, header_size, entries[KEY_PATH_COMPONENTS])
    if not isinstance(raw_components, list) or not raw_components:
        raise BookmarkParseError("path-components record is empty or not an array")
    components: list[str] = []
    for item in raw_components:
        if not isinstance(item, str):
            raise BookmarkParseError(
                f"path component is {type(item).__name__}, expected str"
            )
        components.append(item)
    return components


def _extract_volume(
    blob: bytes, header_size: int, entries: dict[int, int]
) -> tuple[str, str | None]:
    volume_path = "/"
    if KEY_VOLUME_PATH in entries:
        value = _read_value(blob, header_size, entries[KEY_VOLUME_PATH])
        if not isinstance(value, str):
            raise BookmarkParseError(
                f"volume path is {type(value).__name__}, expected str"
            )
        volume_path = value

    volume_name: str | None = None
    if KEY_VOLUME_NAME in entries:
        value = _read_value(blob, header_size, entries[KEY_VOLUME_NAME])
        if isinstance(value, str):
            volume_name = value
    return volume_path, volume_name


def parse_bookmark_path(blob: bytes) -> BookmarkPath:
    """Decode ``blob`` into an absolute path. Raises on anything unexpected.

    The path is ``volume_path`` joined with the ``0x1004`` component array,
    normalised to a single leading slash. Components are used verbatim (no
    Unicode renormalisation here) so the caller owns the NFC/NFD decision --
    see :mod:`apps.mik.match`.
    """
    if not isinstance(blob, (bytes, bytearray)):
        raise BookmarkParseError(
            f"expected bytes, got {type(blob).__name__}"
        )
    blob = bytes(blob)
    header_size = _validate_header(blob)

    entries = _toc_entries(blob, header_size)
    components = _extract_components(blob, header_size, entries)
    volume_path, volume_name = _extract_volume(blob, header_size, entries)

    joined = "/".join(components)
    path = f"{volume_path.rstrip('/')}/{joined}"
    if not path.startswith("/"):
        path = "/" + path
    return BookmarkPath(
        path=path,
        volume_path=volume_path,
        volume_name=volume_name,
        components=tuple(components),
    )


__all__ = [
    "KEY_PATH_COMPONENTS",
    "KEY_VOLUME_PATH",
    "MAGIC",
    "BookmarkParseError",
    "BookmarkPath",
    "parse_bookmark_path",
]
