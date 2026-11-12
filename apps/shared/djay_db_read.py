"""Part of `djay_db.py`, split out with no behavior change: read."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

from apps.shared.numeric_coerce import coerce_optional

if TYPE_CHECKING:
    from apps.shared.normalised import NormalisedAnalysis
from apps.shared.djay_db_tsaf import (
    _CUE_CLASS,
    _LOOP_CLASS,
    _connect_ro,
    _iter_mediaitem_user_data,
    _tsaf_kv,
    _tsaf_parse_cue_element,
    _tsaf_scan_array,
    extract_float32_before_field,
)


def _loop_length_msec(length_raw) -> int | None:
    """The loop length in ms, or None when it is missing, or is not at least one millisecond the store can hold.

    The sign is judged before rounding, as the position is, so -0.0004 seconds is no length, and a positive
    length that rounds to zero is none either.
    """
    try:
        seconds = float(length_raw)
        length_msec = round(seconds * 1000.0)
    except (TypeError, ValueError, OverflowError):  # a missing, NaN or infinite length is no length
        return None
    if seconds <= 0 or length_msec < 1 or length_msec > 2**63 - 1:
        return None
    return length_msec


def _fields_to_cue(fields: dict):
    """Map decoded element fields to a ``NormalisedCue``.

    djay stores cue position in seconds (float32); we convert to milliseconds.
    """
    from .normalised import CueKind, NormalisedCue
    from .rb_color_palette import color_index_to_rgb

    pos = fields.get("position")
    if pos is None:
        return None
    try:
        seconds = float(pos)
        position_msec = round(seconds * 1000.0)
    except (TypeError, ValueError, OverflowError):  # NaN or infinity is no position
        return None
    # A negative position is no position, even one that rounds to zero, so the sign is judged before rounding.
    if seconds < 0 or abs(position_msec) > 2**63 - 1:  # a float32 can hold more ms than the store can
        return None

    classname = str(fields.get("class", ""))
    if classname not in (_CUE_CLASS.decode("utf-8"), _LOOP_CLASS.decode("utf-8")):  # an unknown or unreadable class
        return None
    idx_raw = fields.get("index")
    color_raw = fields.get("color")
    name_raw = fields.get("name")
    length_raw = fields.get("length")

    kind: CueKind
    if classname == _LOOP_CLASS.decode("utf-8"):
        kind = "loop"
        index: int | None = None
    elif isinstance(idx_raw, int) and 0 <= idx_raw <= 7:
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
    if kind == "loop":
        loop_length = _loop_length_msec(length_raw)
        if loop_length is None:  # a loop with no usable length is a lost loop, as store_cues would drop it
            return None

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
    return parse_cues_with_loss(blob)[0]


def parse_cues_with_loss(blob: bytes) -> tuple[list, int]:
    """Like :func:`parse_cues_from_blob`, plus how many of the blob's
    ``cuePoints`` / ``loopRegions`` arrays could not be read in full.

    An array the blob names but whose layout does not scan, whose elements
    stop parsing before its declared count, or which holds an element with
    no usable position, is counted: its cues
    are missing from the result, and the caller must not report it as a
    track with no cues.
    """
    cues: list = []
    unreadable = 0
    for field_name in (b"cuePoints", b"loopRegions"):
        span = _tsaf_scan_array(blob, field_name)
        if span is None:
            if blob and b"\x08" + field_name + b"\x00" in blob:
                unreadable += 1
            continue
        start, end, count = span
        cursor = start + 5
        consumed = 0
        rejected = False
        while cursor < end and consumed < count:
            parsed = _tsaf_parse_cue_element(blob, cursor, limit=end)
            if parsed is None:
                break
            fields, cursor = parsed
            cue = _fields_to_cue(fields)
            if cue is not None:
                cues.append(cue)
            else:
                # The element walked but has no usable position: the cue
                # djay saved is lost, so the array counts as unreadable.
                rejected = True
            consumed += 1
        if rejected:
            unreadable += 1
        elif consumed < count:
            # Not reachable with today's scanner, which already refuses an
            # array whose elements it cannot walk; kept so a future drift
            # between the scanner and this parser shows up as a count.
            unreadable += 1
    cues.sort(
        key=lambda c: (
            c.position_msec,
            c.kind,
            c.index if c.index is not None else -1,
        )
    )
    return cues, unreadable


def iter_cues(db_path: Path, uuid: str) -> list:
    """Return the normalised cue list for a djay track by UUID."""
    con = _connect_ro(db_path)
    try:
        row = con.execute(
            "SELECT data FROM database2 WHERE collection = 'mediaItemUserData' AND key = ? LIMIT 1",
            (uuid,),
        ).fetchone()
    finally:
        con.close()
    if not row or not row[0]:
        return []
    return parse_cues_from_blob(row[0])


# --- Analysis parity ---

_DJAY_KEY_IDX_TO_STD: dict[int, str] = {
    0: "C",
    1: "Db",
    2: "D",
    3: "Eb",
    4: "E",
    5: "F",
    6: "Gb",
    7: "G",
    8: "Ab",
    9: "A",
    10: "Bb",
    11: "B",
    12: "Cm",
    13: "Dbm",
    14: "Dm",
    15: "Ebm",
    16: "Em",
    17: "Fm",
    18: "Gbm",
    19: "Gm",
    20: "Abm",
    21: "Am",
    22: "Bbm",
    23: "Bm",
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


def _normalised_analysis_row(
    uuid_s: str,
    bpm: float | int | str | None,
    manual_bpm: float | int | str | None,
    key_idx: object,
    straight: object,
    user_by_uuid: dict[str, dict],
    tags_by_uuid: dict[str, str],
) -> NormalisedAnalysis | None:
    from .normalised import NormalisedAnalysis

    if not uuid_s:
        return None
    color_idx = (user_by_uuid.get(uuid_s) or {}).get("color_index")
    return NormalisedAnalysis(
        uuid_or_id=uuid_s,
        source="djay",
        bpm=float(bpm) if bpm is not None else None,
        manual_bpm=float(manual_bpm) if manual_bpm is not None else None,
        key_camelot=_djay_key_idx_to_camelot(key_idx),
        color_index=int(color_idx) if color_idx is not None else None,
        tags=tags_by_uuid.get(uuid_s),
        is_straight_grid=bool(straight) if straight is not None else None,
    )


def _mediaitem_tags_by_uuid(con: sqlite3.Connection) -> dict[str, str]:
    tags_by_uuid: dict[str, str] = {}
    for row_key, data in con.execute("SELECT key, data FROM database2 WHERE collection = 'mediaItemUserData'"):
        kv = _tsaf_kv(data)
        uuid = kv.get("uuid") or row_key or ""
        if not uuid:
            continue
        tag = kv.get("tags")
        if tag:
            tags_by_uuid[uuid] = tag
    return tags_by_uuid


def _analysis_from_tsaf_blob(
    row_key: object,
    data: bytes,
    user_by_uuid: dict[str, dict],
    tags_by_uuid: dict[str, str],
) -> NormalisedAnalysis | None:
    kv = _tsaf_kv(data)
    uuid_s = kv.get("uuid") or row_key or ""
    bpm = extract_float32_before_field(data, b"bpm")
    manual_bpm = extract_float32_before_field(data, b"manualBPM")
    key_idx = coerce_optional(kv.get("keySignatureIndex"), int)
    straight = _extract_bool_marker(data, b"isStraightGrid")
    return _normalised_analysis_row(
        str(uuid_s) if uuid_s else "",
        bpm,
        manual_bpm,
        key_idx,
        straight,
        user_by_uuid,
        tags_by_uuid,
    )


def _iter_uuid_index_rows(
    con: sqlite3.Connection,
    optional_index_cols: list[str],
    user_by_uuid: dict[str, dict],
    tags_by_uuid: dict[str, str],
) -> Iterator:
    select_cols = ["uuid", *optional_index_cols]
    sql = f"SELECT {', '.join(select_cols)} FROM secondaryIndex_mediaItemAnalyzedDataIndex"
    for row in con.execute(sql):
        data = dict(zip(select_cols, row, strict=False))
        item = _normalised_analysis_row(
            str(data.get("uuid") or ""),
            data.get("bpm"),
            data.get("manualBPM"),
            data.get("keySignatureIndex"),
            data.get("isStraightGrid"),
            user_by_uuid,
            tags_by_uuid,
        )
        if item is not None:
            yield item


def _iter_rowid_index_rows(
    con: sqlite3.Connection,
    optional_index_cols: list[str],
    user_by_uuid: dict[str, dict],
    tags_by_uuid: dict[str, str],
) -> Iterator:
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
        item = _normalised_analysis_row(
            str(data.get("key") or ""),
            data.get("bpm"),
            data.get("manualBPM"),
            data.get("keySignatureIndex"),
            data.get("isStraightGrid"),
            user_by_uuid,
            tags_by_uuid,
        )
        if item is not None:
            yield item


def _iter_analysis_from_secondary_index(
    con: sqlite3.Connection,
    user_by_uuid: dict[str, dict],
    tags_by_uuid: dict[str, str],
) -> Iterator | None:
    try:
        have_index = bool(
            con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='secondaryIndex_mediaItemAnalyzedDataIndex'"
            ).fetchone()
        )
    except sqlite3.DatabaseError:
        return None
    if not have_index:
        return None
    cols = {row[1] for row in con.execute("PRAGMA table_info(secondaryIndex_mediaItemAnalyzedDataIndex)")}
    optional_index_cols = [name for name in ("bpm", "manualBPM", "keySignatureIndex", "isStraightGrid") if name in cols]
    if "uuid" in cols:
        return _iter_uuid_index_rows(con, optional_index_cols, user_by_uuid, tags_by_uuid)
    if "rowid" in cols:
        return _iter_rowid_index_rows(con, optional_index_cols, user_by_uuid, tags_by_uuid)
    return None


def iter_analysis(db_path: Path) -> Iterator:
    """Yield ``NormalisedAnalysis`` for every analysed djay track.

    Fast path: ``secondaryIndex_mediaItemAnalyzedDataIndex`` when present.
    Fallback: scan ``mediaItemAnalyzedData`` TSAF blobs directly.
    """
    con = _connect_ro(db_path)
    try:
        user_by_uuid = _iter_mediaitem_user_data(con)
        tags_by_uuid = _mediaitem_tags_by_uuid(con)
        indexed = _iter_analysis_from_secondary_index(con, user_by_uuid, tags_by_uuid)
        if indexed is not None:
            yield from indexed
            return
        for row_key, data in con.execute("SELECT key, data FROM database2 WHERE collection = 'mediaItemAnalyzedData'"):
            item = _analysis_from_tsaf_blob(row_key, data, user_by_uuid, tags_by_uuid)
            if item is not None:
                yield item
    finally:
        con.close()
