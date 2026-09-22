"""Read-only reader for Algoriddim djay Pro's ``MediaLibrary.db`` (v2).

Format: YapDatabase (plain sqlite) keyed-collection store, with values encoded
in a proprietary **TSAF** binary format. See ``docs/djay_db_schema_v2.md`` for
full format notes.

v2 upgrades (Phase 2 / SYNC-01):
  * Fixed the ``0x13`` float32 advance bug (was ``i += 9`` → now ``i += 8``).
  * ``extract_rating_from_tsaf`` tail-scan (``0x0f <uint8 1-5>`` in last 30 bytes).
  * ``extract_float32_before_field`` for numeric fields like ``duration``.
  * ``iter_tracks`` joins ``localMediaItemLocations`` + ``globalMediaItemLocations``
    against ``mediaItemUserData`` by UUID to surface ``rating`` + ``play_count``.
  * ``iter_playlists`` resolves ``view_mediaItemPlaylistView_page.data`` int64-LE
    arrays into ordered ``track_uuids`` lists.

This reader is ALWAYS read-only. All writes live in ``apps/sync/*`` (Phase 2 / 3 / 4).
"""
from __future__ import annotations

import sqlite3
import struct
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlparse

# ----- Write-path safety contract (INFRA-02) ----------------------------
#
# This module is read-only by construction (``_connect_ro`` opens the djay DB
# with ``mode=ro&immutable=1``). There are currently **zero** write-capable
# functions, and that is enforced below.
#
# If a future contributor adds a function that mutates the djay database, they
# MUST:
#   1. Add its name to ``_REQUIRES_SAFETY_SESSION``.
#   2. Give it a ``safety_session`` parameter (validated at runtime below).
#   3. Route all writes through the safety-session guard in ``apps/sync``.
#
# The module-level assertion at import time guarantees the contract cannot
# silently drift: if the set is non-empty, every listed function must exist
# in this module and must accept ``safety_session``.
_REQUIRES_SAFETY_SESSION: frozenset[str] = frozenset()


def _enforce_write_path_contract() -> None:
    """Validate the write-path contract at import time.

    For every name in :data:`_REQUIRES_SAFETY_SESSION`, require that:

    * the function exists in this module, and
    * it accepts a parameter named ``safety_session``.

    Raises ``RuntimeError`` at import time if the contract is violated, so
    drift is caught before any caller can bypass the safety rails.
    """
    import inspect

    mod_globals = globals()
    for name in _REQUIRES_SAFETY_SESSION:
        fn = mod_globals.get(name)
        if fn is None or not callable(fn):
            raise RuntimeError(
                f"djay_db write-path contract violated: "
                f"{name!r} listed in _REQUIRES_SAFETY_SESSION but not defined"
            )
        sig = inspect.signature(fn)
        if "safety_session" not in sig.parameters:
            raise RuntimeError(
                f"djay_db write-path contract violated: "
                f"{name!r} must accept a 'safety_session' parameter"
            )


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
        elif b == 0x2b and i + 1 < n and blob[i + 1] == 0x08:
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
        elif b == 0x2d or b == 0x05 or b == 0x0f:
            # enum / small-int: 1 tag byte + 1 data byte
            prev_str = None
            i += 2
        elif b == 0x0b:  # array header (approx 5 bytes)
            prev_str = None
            i += 5
        else:
            i += 1

    return out


def extract_strings_from_tsaf(data: bytes) -> list[tuple[int, str]]:
    """Extract null-terminated strings preceded by ``0x08`` marker from TSAF blob.

    Returns a list of ``(offset, value)`` tuples so callers can inspect both
    the position and the decoded UTF-8 content. Invalid bytes are skipped.
    The parser retains the companion implementation's byte-level behavior.
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

    Private corpus measurements are not public acceptance evidence.
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
    rating: int = 0               # 0-5; 0 means unrated
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
    if (
        len(decoded_path) >= 4
        and decoded_path[0] == "/"
        and decoded_path[1].isalpha()
        and decoded_path[2:4] == ":/"
    ):
        decoded_path = decoded_path[1:]
    if parsed.netloc and parsed.netloc.lower() != "localhost":
        decoded_path = f"//{parsed.netloc}{decoded_path}"
    return Path(decoded_path)


def _maybe_int(value: str | None) -> int | None:
    """Coerce a TSAF string-valued integer to int. None/empty → None."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _iter_mediaitem_user_data(con: sqlite3.Connection) -> dict[str, dict]:
    """Preload ``mediaItemUserData`` keyed by uuid.

    Returns a mapping ``{uuid: {"rating": int, "play_count": int,
    "color_index": int | None}}``. UUID resolution prefers the TSAF blob's
    own ``uuid`` field; if absent we fall back to ``database2.key`` (which
    is the same string in practice).
    """
    out: dict[str, dict] = {}
    query = (
        "SELECT key, data FROM database2 "
        "WHERE collection = 'mediaItemUserData'"
    )
    for row_key, data in con.execute(query):
        kv = _tsaf_kv(data)
        uuid = kv.get("uuid") or row_key or ""
        if not uuid:
            continue
        rating = extract_rating_from_tsaf(data)
        play_count = _maybe_int(kv.get("playCount")) or 0
        color_index = _maybe_int(kv.get("colorIndex"))
        out[uuid] = {
            "rating": rating,
            "play_count": play_count,
            "color_index": color_index,
        }
    return out


def iter_tracks(db_path: Path) -> Iterator[DjayTrack]:
    """Yield every track (local + streaming) from the djay library.

    Merges ``localMediaItemLocations`` + ``globalMediaItemLocations`` and
    joins against ``mediaItemUserData`` by UUID so each ``DjayTrack`` carries
    its rating + play-count + color index + duration.
    """
    con = _connect_ro(db_path)
    try:
        user_by_uuid = _iter_mediaitem_user_data(con)
        query = (
            "SELECT collection, key, data FROM database2 "
            "WHERE collection IN ('localMediaItemLocations', 'globalMediaItemLocations')"
        )
        for collection, row_key, data in con.execute(query):
            kv = _tsaf_kv(data)
            uuid = kv.get("uuid") or row_key or ""
            source = kv.get("sourceURIs", "") or ""
            is_local = (
                collection == "localMediaItemLocations"
                or source.startswith("file://")
            )
            file_path = _file_uri_to_path(source) if is_local else None
            duration_s = extract_float32_before_field(data, b"duration")
            user = user_by_uuid.get(uuid, {})
            yield DjayTrack(
                uuid=uuid,
                title=kv.get("title", "") or "",
                artist=kv.get("artist", "") or "",
                isrc=kv.get("isrc", "") or "",
                source_uri=source,
                file_path=file_path,
                is_local=is_local,
                rating=user.get("rating", 0),
                duration_s=duration_s,
                play_count=user.get("play_count", 0),
                color_index=user.get("color_index"),
            )
    finally:
        con.close()


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


def _unpack_page_rowids(blob: bytes | None) -> list[int]:
    """Unpack a ``view_mediaItemPlaylistView_page.data`` BLOB into rowid list.

    Returns empty list on None or unaligned data (not a multiple of 8 bytes).
    """
    if not blob:
        return []
    n = len(blob)
    if n % 8 != 0:
        # Unexpected shape -- bail quietly rather than raising.
        return []
    count = n // 8
    if count == 0:
        return []
    return list(struct.unpack(f"<{count}q", blob))


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
            "SELECT key, data FROM database2 "
            "WHERE collection = 'view_mediaItemPlaylistView_page'"
        ).fetchall()
    except sqlite3.DatabaseError:
        return {}
    if not rows:
        return {}

    rowid_to_uuid = _rowid_to_uuid_map(con)
    out: dict[str, list[str]] = {}
    for key, data in rows:
        rowids = _unpack_page_rowids(data)
        uuids = [rowid_to_uuid[r] for r in rowids if r in rowid_to_uuid]
        # Multiple pages may share a key; append in iteration order.
        out.setdefault(key, []).extend(uuids)
    return out


def iter_playlists(db_path: Path) -> Iterator[DjayPlaylist]:
    """Yield every playlist from ``mediaItemPlaylists`` + resolved membership.

    Membership comes from ``view_mediaItemPlaylistView_page.data`` (packed
    int64-LE arrays of ``database2.rowid``). Playlists whose UUID is not
    found in the page view are still yielded with ``track_uuids=[]``.
    """
    con = _connect_ro(db_path)
    try:
        membership = _playlist_membership(con)
        query = (
            "SELECT key, data FROM database2 WHERE collection = 'mediaItemPlaylists'"
        )
        for row_key, data in con.execute(query):
            kv = _tsaf_kv(data)
            uuid = kv.get("uuid") or row_key or ""
            parent = kv.get("parentUUID") or kv.get("parent")
            yield DjayPlaylist(
                uuid=uuid,
                name=kv.get("name", "") or "",
                type=kv.get("type", "") or "",
                parent_uuid=parent if parent else None,
                track_uuids=list(membership.get(uuid, []) or membership.get(row_key, [])),
            )
    finally:
        con.close()


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
                elif b == 0x08:
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


def _tsaf_parse_cue_element(blob: bytes, start: int) -> tuple[dict, int] | None:
    """Parse one cue-point or loop-region nested element starting at ``start``.

    Returns ``(fields, next_offset)`` where ``fields`` carries the decoded
    tokens (``class``, ``position``, ``index``, ``color``, ``name``,
    ``length``). Returns ``None`` if ``start`` doesn't point at ``0x2b``.
    """
    n = len(blob)
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
    while cursor < n and blob[cursor] != 0x2B:
        b = blob[cursor]
        if b == 0x00:
            cursor += 1
            break
        if b == 0x08:
            k = blob.find(b"\x00", cursor + 1)
            if k < 0:
                break
            try:
                s = blob[cursor + 1 : k].decode("utf-8")
            except UnicodeDecodeError:
                s = ""
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
    return fields, cursor


def _fields_to_cue(fields: dict):
    """Map decoded element fields to a ``NormalisedCue``.

    djay stores cue position in seconds (float32); we convert to milliseconds.
    """
    from .normalised import NormalisedCue
    from .rb_color_palette import color_index_to_rgb

    pos = fields.get("position")
    if pos is None:
        return None
    try:
        position_msec = round(float(pos) * 1000.0)
    except (TypeError, ValueError):
        return None

    classname = str(fields.get("class", ""))
    idx_raw = fields.get("index")
    color_raw = fields.get("color")
    name_raw = fields.get("name")
    length_raw = fields.get("length")

    if classname == _LOOP_CLASS.decode("utf-8"):
        kind = "loop"
        index: int | None = None
    else:
        if isinstance(idx_raw, int) and 0 <= idx_raw <= 7:
            kind = "hot"
            index = idx_raw
        else:
            kind = "memory"
            index = None

    try:
        color_int = int(color_raw) if color_raw is not None else None
    except (TypeError, ValueError):
        color_int = None
    color_rgb = color_index_to_rgb(color_int) if color_int else None

    loop_length: int | None = None
    if kind == "loop" and length_raw is not None:
        try:
            loop_length = round(float(length_raw) * 1000.0)
        except (TypeError, ValueError):
            loop_length = None

    name = str(name_raw) if isinstance(name_raw, str) and name_raw else None

    return NormalisedCue(
        position_msec=position_msec,
        kind=kind,
        index=index,
        color_rgb=color_rgb,
        name=name,
        loop_length_msec=loop_length,
    )


def parse_cues_from_blob(blob: bytes) -> list:
    """Parse all cue + loop elements from a ``mediaItemUserData`` TSAF blob."""
    cues: list = []
    for field_name in (b"cuePoints", b"loopRegions"):
        span = _tsaf_scan_array(blob, field_name)
        if span is None:
            continue
        start, end, count = span
        cursor = start + 5
        consumed = 0
        while cursor < end and consumed < count:
            parsed = _tsaf_parse_cue_element(blob, cursor)
            if parsed is None:
                break
            fields, cursor = parsed
            cue = _fields_to_cue(fields)
            if cue is not None:
                cues.append(cue)
            consumed += 1
    cues.sort(
        key=lambda c: (
            c.position_msec,
            c.kind,
            c.index if c.index is not None else -1,
        )
    )
    return cues


def iter_cues(db_path: Path, uuid: str) -> list:
    """Return the normalised cue list for a djay track by UUID."""
    con = _connect_ro(db_path)
    try:
        row = con.execute(
            "SELECT data FROM database2 "
            "WHERE collection = 'mediaItemUserData' AND key = ? LIMIT 1",
            (uuid,),
        ).fetchone()
    finally:
        con.close()
    if not row or not row[0]:
        return []
    return parse_cues_from_blob(row[0])


# --- Analysis parity ---

_DJAY_KEY_IDX_TO_STD: dict[int, str] = {
    0: "C", 1: "Db", 2: "D", 3: "Eb", 4: "E", 5: "F",
    6: "Gb", 7: "G", 8: "Ab", 9: "A", 10: "Bb", 11: "B",
    12: "Cm", 13: "Dbm", 14: "Dm", 15: "Ebm", 16: "Em", 17: "Fm",
    18: "Gbm", 19: "Gm", 20: "Abm", 21: "Am", 22: "Bbm", 23: "Bm",
}


def _djay_key_idx_to_camelot(idx) -> str | None:
    if idx is None:
        return None
    try:
        std = _DJAY_KEY_IDX_TO_STD.get(int(idx))
    except (TypeError, ValueError):
        return None
    if not std:
        return None
    from .harmonic import key_to_camelot

    try:
        return str(key_to_camelot(std))
    except ValueError:
        return None


def _extract_bool_marker(blob: bytes, field: bytes) -> bool | None:
    """Best-effort TSAF boolean detector using the ``0x0d`` false marker."""
    if not blob or not field:
        return None
    needle = b"\x08" + field + b"\x00"
    p = blob.find(needle)
    if p <= 0:
        return None
    return blob[p - 1] != 0x0D


def iter_analysis(db_path: Path) -> Iterator:
    """Yield ``NormalisedAnalysis`` for every analysed djay track.

    Fast path: ``secondaryIndex_mediaItemAnalyzedDataIndex`` when present.
    Fallback: scan ``mediaItemAnalyzedData`` TSAF blobs directly.
    """
    from .normalised import NormalisedAnalysis

    con = _connect_ro(db_path)
    try:
        user_by_uuid = _iter_mediaitem_user_data(con)
        tags_by_uuid: dict[str, str] = {}
        for row_key, data in con.execute(
            "SELECT key, data FROM database2 WHERE collection = 'mediaItemUserData'"
        ):
            kv = _tsaf_kv(data)
            uuid = kv.get("uuid") or row_key or ""
            if not uuid:
                continue
            tag = kv.get("tags")
            if tag:
                tags_by_uuid[uuid] = tag

        have_index = False
        try:
            have_index = bool(
                con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name='secondaryIndex_mediaItemAnalyzedDataIndex'"
                ).fetchone()
            )
        except sqlite3.DatabaseError:
            have_index = False

        if have_index:
            # Discover the available columns; not every djay build emits the
            # same set (manualBPM + isStraightGrid may be absent). Real libraries
            # often index by rowid joined to database2.key, not a uuid column
            # (#3036).
            cols = {
                row[1]
                for row in con.execute(
                    "PRAGMA table_info(secondaryIndex_mediaItemAnalyzedDataIndex)"
                )
            }
            optional_index_cols = [
                name
                for name in ("bpm", "manualBPM", "keySignatureIndex", "isStraightGrid")
                if name in cols
            ]

            def _yield_from_index_row(
                uuid_s: str,
                bpm: object,
                manual_bpm: object,
                key_idx: object,
                straight: object,
            ) -> Iterator:
                if not uuid_s:
                    return
                color_idx = (user_by_uuid.get(uuid_s) or {}).get("color_index")
                yield NormalisedAnalysis(
                    uuid_or_id=uuid_s,
                    source="djay",
                    bpm=float(bpm) if bpm is not None else None,
                    manual_bpm=float(manual_bpm) if manual_bpm is not None else None,
                    key_camelot=_djay_key_idx_to_camelot(key_idx),
                    energy=int(color_idx) if color_idx is not None else None,
                    tags=tags_by_uuid.get(uuid_s),
                    is_straight_grid=bool(straight) if straight is not None else None,
                )

            if "uuid" in cols:
                select_cols = ["uuid", *optional_index_cols]
                sql = (
                    f"SELECT {', '.join(select_cols)} "
                    "FROM secondaryIndex_mediaItemAnalyzedDataIndex"
                )
                for row in con.execute(sql):
                    data = dict(zip(select_cols, row, strict=False))
                    yield from _yield_from_index_row(
                        str(data.get("uuid") or ""),
                        data.get("bpm"),
                        data.get("manualBPM"),
                        data.get("keySignatureIndex"),
                        data.get("isStraightGrid"),
                    )
                return

            if "rowid" in cols:
                select_cols = ["key", *optional_index_cols]
                sql = (
                    f"SELECT d.key, {', '.join(f's.{c}' for c in optional_index_cols)} "
                    "FROM database2 d "
                    "JOIN secondaryIndex_mediaItemAnalyzedDataIndex s "
                    "ON d.rowid = s.rowid "
                    "WHERE d.collection = 'mediaItemAnalyzedData'"
                )
                if not optional_index_cols:
                    sql = (
                        "SELECT d.key "
                        "FROM database2 d "
                        "JOIN secondaryIndex_mediaItemAnalyzedDataIndex s "
                        "ON d.rowid = s.rowid "
                        "WHERE d.collection = 'mediaItemAnalyzedData'"
                    )
                for row in con.execute(sql):
                    data = dict(zip(select_cols, row, strict=False))
                    yield from _yield_from_index_row(
                        str(data.get("key") or ""),
                        data.get("bpm"),
                        data.get("manualBPM"),
                        data.get("keySignatureIndex"),
                        data.get("isStraightGrid"),
                    )
                return

        # Fallback: parse TSAF blobs directly.
        for row_key, data in con.execute(
            "SELECT key, data FROM database2 WHERE collection = 'mediaItemAnalyzedData'"
        ):
            kv = _tsaf_kv(data)
            uuid_s = kv.get("uuid") or row_key or ""
            if not uuid_s:
                continue
            bpm = extract_float32_before_field(data, b"bpm")
            manual_bpm = extract_float32_before_field(data, b"manualBPM")
            key_idx = _maybe_int(kv.get("keySignatureIndex"))
            color_idx = (user_by_uuid.get(uuid_s) or {}).get("color_index")
            straight = _extract_bool_marker(data, b"isStraightGrid")
            yield NormalisedAnalysis(
                uuid_or_id=uuid_s,
                source="djay",
                bpm=float(bpm) if bpm is not None else None,
                manual_bpm=float(manual_bpm) if manual_bpm is not None else None,
                key_camelot=_djay_key_idx_to_camelot(key_idx),
                energy=int(color_idx) if color_idx is not None else None,
                tags=tags_by_uuid.get(uuid_s),
                is_straight_grid=straight,
            )
    finally:
        con.close()


__all__ = [
    "DjayTrack",
    "DjayPlaylist",
    "iter_tracks",
    "iter_playlists",
    "iter_cues",
    "iter_analysis",
    "parse_cues_from_blob",
    "extract_rating_from_tsaf",
    "extract_strings_from_tsaf",
    "extract_float32_before_field",
    "_tsaf_kv",
    "_tsaf_scan_array",
    "_tsaf_parse_cue_element",
]


# ----- Contract enforcement (runs at import time) -----------------------

_enforce_write_path_contract()


# ----- Smoke block -------------------------------------------------------


def _smoke_main() -> None:
    """Rich summary smoke test. Copies live DB into working copy first.

    Run via ``python -m apps.shared.djay_db``.
    """
    from collections import Counter

    from rich.console import Console
    from rich.table import Table

    from . import paths

    console = Console(width=120)
    copied = paths.copy_live_dbs()
    target = copied.get("djay")
    if target is None:
        console.print(
            "[yellow]djay live DB not present at "
            f"{paths.DJAY_LIVE_DB}; nothing to smoke-test.[/yellow]"
        )
        return
    console.print(f"[green]Reading[/green] {target}")
    tracks = list(iter_tracks(target))
    playlists = list(iter_playlists(target))

    local = [t for t in tracks if t.is_local]
    streaming = [t for t in tracks if not t.is_local]
    rating_dist = Counter(t.rating for t in tracks)
    with_duration = [t for t in tracks if t.duration_s]

    table = Table(title="djay library summary (v2)")
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("total tracks", str(len(tracks)))
    table.add_row("local", str(len(local)))
    table.add_row("streaming", str(len(streaming)))
    table.add_row("with duration", str(len(with_duration)))
    for star in sorted(k for k in rating_dist if k > 0):
        table.add_row(f"rated {star}-star", str(rating_dist[star]))
    table.add_row("rated (total)", str(sum(v for k, v in rating_dist.items() if k > 0)))
    table.add_row("playlists", str(len(playlists)))
    non_empty = [p for p in playlists if p.track_uuids]
    table.add_row("playlists with members", str(len(non_empty)))
    console.print(table)


if __name__ == "__main__":  # pragma: no cover
    _smoke_main()
