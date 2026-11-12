"""Part of `djay_db.py`, split out with no behavior change: tsaf."""

from __future__ import annotations

import sqlite3
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlparse

from apps.shared.djay_page_data import parse_page_data
from apps.shared.numeric_coerce import coerce_optional

if TYPE_CHECKING:
    pass


# ----- TSAF parsing -----------------------------------------------------


def _tsaf_kv(blob: bytes) -> dict[str, str]:
    """Extract ``{key: value}`` pairs of string-valued fields from a TSAF blob.

    Best-effort: we only recognize ``0x08``-prefixed NUL-terminated UTF-8
    strings and pair two such strings as ``(value, key)`` when they are
    byte-adjacent (separated by at most two NUL padding bytes). Nested-object
    markers (``0x2b 0x08 <classname> 0x00``), float32 (``0x13``), enums
    (``0x2d``), small ints (``0x05``/``0x0f``) and array headers (``0x0b``)
    reset the adjacency tracker but do not break the outer pair sequence, so
    array-wrapped scalars (e.g. ``sourceURIs``) still resolve correctly.

    Returns an empty dict if the blob lacks the ``TSAF`` header.
    """
    out: dict[str, str] = {}
    if len(blob) < 16 or blob[:4] != b"TSAF":
        return out

    n = len(blob)
    i = 16  # skip 4-byte magic + version + flags + unknown + field_count header
    prev_str: tuple[str, int] | None = None  # (value, end_offset_exclusive)

    while i < n:
        b = blob[i]
        if b == 0x08:  # 0x08 <utf8> 0x00 -- a string
            j = blob.find(b"\x00", i + 1)
            if j < 0:
                break
            try:
                s = blob[i + 1 : j].decode("utf-8")
            except UnicodeDecodeError:
                s = ""
            adjacent = False
            if prev_str is not None:
                gap = i - prev_str[1]
                # Accept up to 2 NUL padding bytes between value and key.
                if 0 <= gap <= 2 and all(c == 0 for c in blob[prev_str[1] : i]):
                    adjacent = True
            if adjacent and prev_str is not None:
                value, key = prev_str[0], s
                if key and key not in out:
                    out[key] = value
                prev_str = None  # consume; don't chain this key as a value
            else:
                prev_str = (s, j + 1)
            i = j + 1
        elif b == 0x2B and i + 1 < n and blob[i + 1] == 0x08:
            # Nested class marker: 0x2b 0x08 <classname> 0x00 -- not a pair.
            j = blob.find(b"\x00", i + 2)
            if j < 0:
                break
            prev_str = None
            i = j + 1
        elif b == 0x13:
            # float32 value: 1 tag + 3 padding + 4 float = 8 bytes total.
            # Fixed from v1 which used i += 9 (assumed float64).
            prev_str = None
            i += 8
        elif b == 0x2D or b == 0x05 or b == 0x0F:
            # enum / small-int: 1 tag byte + 1 data byte
            prev_str = None
            i += 2
        elif b == 0x0B:  # array header (approx 5 bytes)
            prev_str = None
            i += 5
        else:
            i += 1

    return out


def extract_strings_from_tsaf(data: bytes) -> list[tuple[int, str]]:
    """Extract null-terminated strings preceded by ``0x08`` marker from TSAF blob.

    Returns a list of ``(offset, value)`` tuples so callers can inspect both
    the position and the decoded UTF-8 content. Invalid bytes are skipped.
    Ported verbatim from the companion repo
    (``~/Desktop/music-dj/src/extract_djay.py``) for test parity.
    """
    strings: list[tuple[int, str]] = []
    i = 0
    n = len(data)
    while i < n:
        if data[i] == 0x08:
            end = data.find(b"\x00", i + 1)
            if 0 < end < i + 500:
                try:
                    s = data[i + 1 : end].decode("utf-8")
                    strings.append((i, s))
                except UnicodeDecodeError:
                    pass
                i = end + 1
                continue
        i += 1
    return strings


def extract_rating_from_tsaf(data: bytes) -> int:
    """Extract star rating from a TSAF blob tail.

    Scans the last 30 bytes for the first ``0x0f <uint8>`` pair whose uint8
    value is in the range 1..5. Returns ``0`` if no rating marker is present.

    Ported verbatim from the companion repo
    (``~/Desktop/music-dj/src/extract_djay.py``) where it matched 310+ rated
    tracks in a real 6,574-row djay library.
    """
    if not data:
        return 0
    tail = data[-30:]
    for i in range(len(tail) - 1):
        if tail[i] == 0x0F:
            val = tail[i + 1]
            if 1 <= val <= 5:
                return val
    return 0


def _tsaf_schema_field_names(blob: bytes) -> list[str]:
    """Return ordered schema field names from a ``mediaItemUserData`` TSAF blob."""
    if len(blob) < 20 or blob[:4] != b"TSAF":
        return []
    header_count = struct.unpack_from("<I", blob, 8)[0]
    uuid_key = blob.find(b"\x08uuid\x00")
    if uuid_key < 0:
        return []
    pos = uuid_key + len(b"\x08uuid\x00")
    while pos < len(blob) and blob[pos] not in (0x0B, 0x08):
        token = blob[pos]
        if token in (0x0F, 0x2D, 0x05) and pos + 1 < len(blob):
            pos += 2
        else:
            pos += 1
    names: list[str] = []
    if pos < len(blob) and blob[pos] == 0x0B:
        count = struct.unpack_from("<I", blob, pos + 1)[0]
        pos += 5
        for _ in range(count):
            if pos >= len(blob) or blob[pos] != 0x08:
                break
            end = blob.find(b"\x00", pos + 1)
            if end < 0:
                break
            names.append(blob[pos + 1 : end].decode("utf-8", errors="replace"))
            pos = end + 1
        return names
    for _ in range(header_count):
        if pos >= len(blob) or blob[pos] != 0x08:
            break
        end = blob.find(b"\x00", pos + 1)
        if end < 0:
            break
        names.append(blob[pos + 1 : end].decode("utf-8", errors="replace"))
        pos = end + 1
    return names


def extract_schema_indexed_uint8(blob: bytes, field_name: str) -> int | None:
    """Read ``0x0f <uint8> 0x05 <schema_idx>`` for a named schema field."""
    names = _tsaf_schema_field_names(blob)
    try:
        field_idx = names.index(field_name)
    except ValueError:
        return None
    n = len(blob)
    for i in range(n - 3):
        if blob[i] == 0x0F and blob[i + 2] == 0x05 and blob[i + 3] == field_idx:
            return int(blob[i + 1])
    return None


def extract_color_index_from_tsaf(blob: bytes) -> int | None:
    """Decode the user's color label (0=none, 1..7) from schema-indexed ``colorIndex``."""
    raw = extract_schema_indexed_uint8(blob, "colorIndex")
    if raw is None or raw == 0:
        return None
    if 1 <= raw <= 7:
        return raw
    return None


def extract_float32_before_field(blob: bytes, field: bytes) -> float | None:
    """Read the little-endian float32 value stored immediately before a field name.

    TSAF stores values before their keys. A float32 value is encoded as
    ``0x13 00 00 00 <4 bytes LE>`` followed by ``0x08 <field> 0x00``.
    Locate ``0x08 <field> 0x00`` and read 4 bytes starting 4 bytes before it.

    Returns ``None`` if the marker or the 8 preceding bytes are unavailable,
    or if the decode raises ``struct.error``.
    """
    if not blob or not field:
        return None
    needle = b"\x08" + field + b"\x00"
    p = blob.find(needle)
    if p < 8:
        return None
    # The 4 bytes immediately before `0x08 <field> 0x00` are the LE float32 value.
    raw = blob[p - 4 : p]
    try:
        return struct.unpack("<f", raw)[0]
    except struct.error:
        return None


# ----- Dataclasses -------------------------------------------------------


@dataclass(slots=True)
class DjayTrack:
    uuid: str
    title: str
    artist: str
    isrc: str
    source_uri: str  # "file:///..." or "spotify:track:..." etc.
    file_path: Path | None  # resolved Path if source is a local file://, else None
    is_local: bool
    # --- new in v2 (SYNC-01 / SYNC-02) ---
    rating: int = 0  # 0-5; 0 means unrated
    duration_s: float | None = None
    play_count: int = 0
    color_index: int | None = None


@dataclass(slots=True)
class DjayPlaylist:
    uuid: str
    name: str
    type: str  # TSAF enum not decoded; left as empty string for now
    parent_uuid: str | None
    track_uuids: list[str] = field(default_factory=list)  # ordered membership (v2)


# ----- Public API --------------------------------------------------------


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)


def _file_uri_to_path(uri: str) -> Path | None:
    if not uri.startswith("file://"):
        return None
    parsed = urlparse(uri)
    decoded_path = unquote(parsed.path)
    if len(decoded_path) >= 4 and decoded_path[0] == "/" and decoded_path[1].isalpha() and decoded_path[2:4] == ":/":
        decoded_path = decoded_path[1:]
    if parsed.netloc and parsed.netloc.lower() != "localhost":
        decoded_path = f"//{parsed.netloc}{decoded_path}"
    return Path(decoded_path)


def _iter_mediaitem_user_data(con: sqlite3.Connection) -> dict[str, dict]:
    """Preload ``mediaItemUserData`` keyed by uuid.

    Returns a mapping ``{uuid: {"rating": int, "play_count": int,
    "color_index": int | None}}``. UUID resolution prefers the TSAF blob's
    own ``uuid`` field; if absent we fall back to ``database2.key`` (which
    is the same string in practice).
    """
    out: dict[str, dict] = {}
    query = "SELECT key, data FROM database2 WHERE collection = 'mediaItemUserData'"
    for row_key, data in con.execute(query):
        kv = _tsaf_kv(data)
        uuid = kv.get("uuid") or row_key or ""
        if not uuid:
            continue
        rating = extract_rating_from_tsaf(data)
        play_count = coerce_optional(kv.get("playCount"), int) or 0
        color_index = extract_color_index_from_tsaf(data)
        out[uuid] = {
            "rating": rating,
            "play_count": play_count,
            "color_index": color_index,
        }
    return out


def _rowid_to_uuid_map(con: sqlite3.Connection) -> dict[int, str]:
    """Build a ``{rowid: uuid}`` mapping across every track collection.

    ``view_mediaItemPlaylistView_page.data`` stores packed int64-LE arrays
    of ``database2.rowid``; resolving them requires this index.
    """
    out: dict[int, str] = {}
    query = (
        "SELECT rowid, key, data FROM database2 "
        "WHERE collection IN "
        "('localMediaItemLocations', 'globalMediaItemLocations', 'mediaItemUserData')"
    )
    for rowid, row_key, data in con.execute(query):
        kv = _tsaf_kv(data)
        uuid = kv.get("uuid") or row_key or ""
        if uuid and rowid not in out:
            out[rowid] = uuid
    return out


def _playlist_membership(con: sqlite3.Connection) -> dict[str, list[str]]:
    """Return ``{playlist_uuid: [track_uuid, ...]}`` resolved from page blobs.

    Strategy: we index the page-view rows by group key (``key`` in
    ``view_mediaItemPlaylistView_page``, which is the playlist UUID by
    convention). For each page blob we decode the packed int64-LE rowid
    array and resolve each rowid to the corresponding track UUID.

    If the view collection is absent (live DB currently has 0 rows), every
    playlist falls back to an empty list.
    """
    # First test whether the view collection exists at all.
    try:
        rows = con.execute(
            "SELECT key, data FROM database2 WHERE collection = 'view_mediaItemPlaylistView_page'"
        ).fetchall()
    except sqlite3.DatabaseError:
        return {}
    if not rows:
        return {}

    rowid_to_uuid = _rowid_to_uuid_map(con)
    out: dict[str, list[str]] = {}
    for key, data in rows:
        rowids = parse_page_data(data)
        uuids = [rowid_to_uuid[r] for r in rowids if r in rowid_to_uuid]
        # Multiple pages may share a key; append in iteration order.
        out.setdefault(key, []).extend(uuids)
    return out


# ----- Phase 4: cue / analysis reader extensions ------------------------
#
# The concrete byte layout for ``cuePoints`` inside a TSAF ``mediaItemUserData``
# blob has not been fully reverse-engineered against a committed fixture
# corpus; see ``docs/djay_db_schema_v2_addendum_cues.md`` for the addendum and
# ``scripts/harvest_djay_cues.py`` for the user-driven runbook that pulls live
# blobs. The functions here implement the schema-v2 pattern that the
# addendum specifies:
#
#   ``\x08cuePoints\x00`` is preceded by a ``0x0b <count uint32 LE>`` array
#   header. Each element is a nested ``0x2b 0x08 ADCMediaItemCuePoint\x00``
#   object containing ``position`` (0x13 float32 seconds), ``index``
#   (0x0f uint8), ``color`` (0x0f uint8), and an optional ``name``
#   (0x08 UTF-8). The element terminates with ``0x00``.
#
# This is the encoding we also emit from ``apps/sync/djay_writer.py`` so the
# reader and writer are round-trip consistent; when/if live fixture harvest
# reveals deviations, update the addendum and the encoders together.


_CUE_CLASS = b"ADCMediaItemCuePoint"
_LOOP_CLASS = b"ADCMediaItemLoopRegion"


def _tsaf_scan_array(blob: bytes, field_name: bytes) -> tuple[int, int, int] | None:
    """Locate a named ``0x0b`` array field in a TSAF blob.

    Returns ``(array_start_offset, value_region_end_offset, count)`` where
    ``array_start_offset`` is the offset of the ``0x0b`` tag byte and
    ``value_region_end_offset`` is the offset of the byte immediately after
    the final element. Returns ``None`` if the field is absent.

    TSAF convention: ``0x08 <field> 0x00`` followed by
    ``0x0b <count uint32 LE>`` plus ``count`` nested-object elements. Each
    element begins with ``0x2b 0x08 <classname> 0x00`` and ends at the next
    ``0x2b`` (start of the following element) or a single ``0x00`` terminator
    after the last element.
    """
    if not blob or not field_name:
        return None
    needle = b"\x08" + field_name + b"\x00"
    p = blob.find(needle)
    if p < 0:
        return None
    arr_start = p + len(needle)
    n = len(blob)
    if arr_start >= n or blob[arr_start] != 0x0B:
        return None
    if arr_start + 5 > n:
        return None
    count = struct.unpack("<I", blob[arr_start + 1 : arr_start + 5])[0]
    cursor = arr_start + 5
    elements_consumed = 0
    element_end = cursor
    while cursor < n and elements_consumed < count:
        if blob[cursor] == 0x2B:
            j = blob.find(b"\x00", cursor + 2)
            if j < 0:
                break
            inner = j + 1
            while inner < n and blob[inner] != 0x2B:
                b = blob[inner]
                if b == 0x00:
                    inner += 1
                    break
                if b == 0x08:
                    k = blob.find(b"\x00", inner + 1)
                    if k < 0:
                        inner = n
                        break
                    inner = k + 1
                elif b == 0x13:
                    inner += 8
                elif b == 0x0F or b == 0x2D or b == 0x05:
                    inner += 2
                elif b == 0x0D:
                    inner += 1
                else:
                    inner += 1
            cursor = inner
            elements_consumed += 1
            element_end = cursor
        else:
            break
    if elements_consumed != count:
        return None
    return (arr_start, element_end, count)


# Top-level TSAF fields that can follow a cue array in ``mediaItemUserData``. A key with one of these names inside
# an element means the element has run past the end of its array into the next field, so the array is unreadable.
_TOP_LEVEL_FIELDS = frozenset({"bpm", "manualBPM", "isStraightGrid", "duration", "cuePoints", "loopRegions"})
_NAME_KEY_FOLLOWS = b"\x08name\x00"


def _tsaf_parse_cue_element(blob: bytes, start: int, limit: int | None = None) -> tuple[dict, int] | None:
    """Parse one cue-point or loop-region nested element starting at ``start``.

    Returns ``(fields, next_offset)`` where ``fields`` carries the decoded
    tokens (``class``, ``position``, ``index``, ``color``, ``name``,
    ``length``). Returns ``None`` if ``start`` doesn't point at ``0x2b``, if the
    element runs past ``limit`` (the end of its array), or if it runs into a
    top-level field, which means the array ended inside this element.
    """
    n = len(blob) if limit is None else min(limit, len(blob))
    if start >= n or blob[start] != 0x2B:
        return None
    j = blob.find(b"\x00", start + 2)
    if j < 0:
        return None
    try:
        classname = blob[start + 2 : j].decode("utf-8")
    except UnicodeDecodeError:
        classname = ""
    cursor = j + 1
    fields: dict[str, object] = {"class": classname}
    prev_str: tuple[str, int] | None = None
    prev_f32: float | None = None
    prev_u8: int | None = None
    terminated = False
    while cursor < n and blob[cursor] != 0x2B:
        b = blob[cursor]
        if b == 0x00:
            cursor += 1
            terminated = True
            break
        if b == 0x08:
            k = blob.find(b"\x00", cursor + 1)
            if k < 0 or k >= n:
                break
            try:
                s = blob[cursor + 1 : k].decode("utf-8")
            except UnicodeDecodeError:
                s = ""
            # A top-level field name is only ours as a name value, which the encoder writes right before its
            # ``name`` key. Anywhere else it is the next field, so this element ran past the end of its array.
            if s in _TOP_LEVEL_FIELDS and blob[k + 1 : k + 1 + len(_NAME_KEY_FOLLOWS)] != _NAME_KEY_FOLLOWS:
                return None
            if prev_str is not None:
                value, key = prev_str[0], s
                if key:
                    fields.setdefault(key, value)
                prev_str = None
            elif prev_f32 is not None:
                fields.setdefault(s, prev_f32)
                prev_f32 = None
            elif prev_u8 is not None:
                fields.setdefault(s, prev_u8)
                prev_u8 = None
            else:
                prev_str = (s, k + 1)
            cursor = k + 1
        elif b == 0x13:
            if cursor + 8 > n:
                break
            prev_f32 = struct.unpack("<f", blob[cursor + 4 : cursor + 8])[0]
            prev_str = None
            prev_u8 = None
            cursor += 8
        elif b == 0x0F or b == 0x05:
            if cursor + 2 > n:
                break
            prev_u8 = blob[cursor + 1]
            prev_str = None
            prev_f32 = None
            cursor += 2
        elif b == 0x2D:
            if cursor + 2 > n:
                break
            prev_u8 = blob[cursor + 1]
            cursor += 2
        elif b == 0x0D:
            cursor += 1
        else:
            cursor += 1
    # A truncated field stops the loop short of the end, so an element is whole only when it reached its terminator
    # or the next element's start byte.
    if not terminated and not (cursor < n and blob[cursor] == 0x2B):
        return None
    return fields, cursor
