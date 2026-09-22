"""djay TSAF preserve-then-patch primitives for Phase 4.

We never reserialize a whole TSAF blob; we splice-replace the affected
region. This module provides:

* ``patch_rating`` -- replace the ``0x0f <1..5>`` tail marker (already
  proven on live data in Phase 2). Insert before the tail terminator when
  absent.
* ``patch_cue_points`` / ``patch_loop_regions`` -- encode a cue/loop array
  and splice it into a ``mediaItemUserData`` blob, replacing any existing
  array or inserting before the terminator.
* ``patch_start_point`` -- write a ``0x13`` float32 value before a
  ``startPoint`` key.
* ``patch_manual_bpm`` / ``patch_key_signature_index`` / ``patch_color_index``
  / ``patch_tags`` -- single-field TSAF value splices for analysis sync.
* ``encode_cue_point`` / ``encode_loop_region`` -- emit a single nested
  element in the format described in
  ``docs/djay_db_schema_v2_addendum_cues.md``.

The encoders and ``apps/shared/djay_db.py``'s decoders are round-trip
consistent: re-parsing a blob we just patched yields the same cue list
we passed in. When live-fixture harvest reveals byte-level deviations
from Algoriddim's encoding, update the addendum and both encoders
together.
"""
from __future__ import annotations

import struct
from collections.abc import Iterable

from apps.shared.normalised import NormalisedCue
from apps.shared.rb_color_palette import rgb_to_color_index

_CUE_CLASS = b"ADCMediaItemCuePoint"
_LOOP_CLASS = b"ADCMediaItemLoopRegion"


# ----- Low-level token emitters ------------------------------------------


def _emit_string(s: str | None) -> bytes:
    """Emit ``0x08 <utf8> 0x00``."""
    if s is None:
        return b""
    return b"\x08" + s.encode("utf-8") + b"\x00"


def _emit_float32(value: float) -> bytes:
    """Emit ``0x13 0x00 0x00 0x00 <float32 LE>`` (8 bytes)."""
    return b"\x13\x00\x00\x00" + struct.pack("<f", float(value))


def _emit_uint8(value: int) -> bytes:
    """Emit ``0x0f <uint8>`` (2 bytes)."""
    v = max(0, min(255, int(value)))
    return b"\x0f" + bytes([v])


# ----- Cue-element encoders ---------------------------------------------


def encode_cue_point(cue: NormalisedCue) -> bytes:
    """Encode one ``ADCMediaItemCuePoint`` nested element.

    Layout (schema v2 addendum):
        0x2b 0x08 ADCMediaItemCuePoint 0x00
        <float32 pos> 0x08 "position" 0x00
        [0x0f <idx>  0x08 "index" 0x00]      # hot cues only
        [0x0f <color> 0x08 "color" 0x00]
        [0x08 <name> 0x00 0x08 "name" 0x00]
        0x00                                  # element terminator
    """
    parts = [b"\x2b" + _emit_string(_CUE_CLASS.decode("utf-8"))]
    pos_s = cue.position_msec / 1000.0
    parts.append(_emit_float32(pos_s) + _emit_string("position"))
    if cue.kind == "hot" and cue.index is not None:
        parts.append(_emit_uint8(int(cue.index)) + _emit_string("index"))
    color_idx = rgb_to_color_index(cue.color_rgb)
    if color_idx:
        parts.append(_emit_uint8(color_idx) + _emit_string("color"))
    if cue.name:
        parts.append(_emit_string(cue.name) + _emit_string("name"))
    parts.append(b"\x00")
    return b"".join(parts)


def encode_loop_region(cue: NormalisedCue) -> bytes:
    """Encode one ``ADCMediaItemLoopRegion`` nested element (has ``length``)."""
    parts = [b"\x2b" + _emit_string(_LOOP_CLASS.decode("utf-8"))]
    pos_s = cue.position_msec / 1000.0
    parts.append(_emit_float32(pos_s) + _emit_string("position"))
    length_s = (cue.loop_length_msec or 0) / 1000.0
    parts.append(_emit_float32(length_s) + _emit_string("length"))
    color_idx = rgb_to_color_index(cue.color_rgb)
    if color_idx:
        parts.append(_emit_uint8(color_idx) + _emit_string("color"))
    if cue.name:
        parts.append(_emit_string(cue.name) + _emit_string("name"))
    parts.append(b"\x00")
    return b"".join(parts)


def encode_cue_array(cues: Iterable[NormalisedCue]) -> bytes:
    """Encode the full ``0x0b <count> <elements...>`` body (no field name)."""
    elements = [encode_cue_point(c) for c in cues]
    body = b"".join(elements)
    return b"\x0b" + struct.pack("<I", len(elements)) + body


def encode_loop_array(loops: Iterable[NormalisedCue]) -> bytes:
    elements = [encode_loop_region(c) for c in loops]
    body = b"".join(elements)
    return b"\x0b" + struct.pack("<I", len(elements)) + body


# ----- Array splice into an existing blob -------------------------------


def _scan_array_span(blob: bytes, field_name: bytes) -> tuple[int, int, int] | None:
    """Locate the existing ``0x0b <count> <elements>`` region for ``field_name``.

    Delegates to the reader's scanner so we stay in lock-step.
    """
    from apps.shared.djay_db import _tsaf_scan_array

    return _tsaf_scan_array(blob, field_name)


def _find_insertion_point(blob: bytes) -> int:
    """Return an offset near the blob tail where a new field can be spliced.

    Strategy: place the new field immediately before the last ``0x0b``
    terminator chunk (schema v2 describes tail pattern ``0x0b 0x05 <x> 0x00``).
    If we can't find one, fall back to just-before the trailing ``0x00`` byte.
    When even that fails (e.g. tiny blob), append at end.
    """
    n = len(blob)
    if n == 0:
        return 0
    # Scan backwards for a ``0x0b 0x05`` sequence in the last 30 bytes.
    start = max(0, n - 60)
    for i in range(n - 2, start - 1, -1):
        if blob[i] == 0x0B and blob[i + 1] == 0x05:
            return i
    # Otherwise insert just before a trailing 0x00.
    if blob[-1] == 0x00:
        return n - 1
    return n


def _patch_array(blob: bytes, field_name: bytes, encoded_array: bytes) -> bytes:
    """Insert or replace a ``0x0b`` array field inside ``blob``."""
    span = _scan_array_span(blob, field_name)
    if span is not None:
        start, end, _count = span
        return blob[:start] + encoded_array + blob[end:]
    # Insert new: prepend the field-name marker plus the array.
    insert_at = _find_insertion_point(blob)
    new_field = b"\x08" + field_name + b"\x00" + encoded_array
    return blob[:insert_at] + new_field + blob[insert_at:]


def patch_cue_points(blob: bytes, new_cues: Iterable[NormalisedCue]) -> bytes:
    """Preserve-then-patch: splice the ``cuePoints`` array in-place."""
    cues = [c for c in new_cues if c.kind != "loop"]
    return _patch_array(blob, b"cuePoints", encode_cue_array(cues))


def patch_loop_regions(blob: bytes, new_loops: Iterable[NormalisedCue]) -> bytes:
    """Preserve-then-patch: splice the ``loopRegions`` array in-place."""
    loops = [c for c in new_loops if c.kind == "loop"]
    return _patch_array(blob, b"loopRegions", encode_loop_array(loops))


# ----- Scalar-field splices (analysis + rating) -------------------------


def patch_rating(blob: bytes, rating: int) -> bytes:
    """Set the tail ``0x0f <1..5>`` rating marker.

    - If an existing marker exists in the last 30 bytes, replace the value.
    - Else insert ``0x0f <rating> 0x08 rating 0x00`` before the tail
      terminator block.
    """
    if not blob:
        return blob
    rating = max(0, min(5, int(rating)))
    tail_start = max(0, len(blob) - 30)
    for i in range(tail_start, len(blob) - 1):
        if blob[i] == 0x0F and 1 <= blob[i + 1] <= 5:
            return blob[: i + 1] + bytes([rating]) + blob[i + 2 :]
    # Insert new.
    insert_at = _find_insertion_point(blob)
    new_field = _emit_uint8(rating) + _emit_string("rating")
    return blob[:insert_at] + new_field + blob[insert_at:]


def _patch_scalar_before_field(
    blob: bytes, field: bytes, emit_value: bytes
) -> bytes:
    """Replace (or insert) the scalar-value bytes immediately before ``field``.

    Scalar fields in TSAF place value bytes directly before ``0x08 <field>
    0x00``. ``emit_value`` is the exact bytes of the value token (e.g. the
    8-byte ``0x13 0x00 0x00 0x00 <float32>`` for float32s).
    """
    needle = b"\x08" + field + b"\x00"
    p = blob.find(needle)
    if p < 0:
        # Insert anew before the tail terminator.
        insert_at = _find_insertion_point(blob)
        return blob[:insert_at] + emit_value + needle + blob[insert_at:]
    value_len = len(emit_value)
    start = p - value_len
    if start < 0:
        start = p  # give up on preserving bytes before field name
    return blob[:start] + emit_value + blob[p:]


def patch_manual_bpm(blob: bytes, bpm: float) -> bytes:
    return _patch_scalar_before_field(blob, b"manualBPM", _emit_float32(bpm))


def patch_key_signature_index(blob: bytes, idx: int) -> bytes:
    return _patch_scalar_before_field(blob, b"keySignatureIndex", _emit_uint8(idx))


def patch_color_index(blob: bytes, idx: int) -> bytes:
    return _patch_scalar_before_field(blob, b"colorIndex", _emit_uint8(idx))


def patch_tags(blob: bytes, tags: str) -> bytes:
    return _patch_scalar_before_field(blob, b"tags", _emit_string(tags))


def patch_start_point(blob: bytes, seconds: float) -> bytes:
    return _patch_scalar_before_field(blob, b"startPoint", _emit_float32(seconds))


__all__ = [
    "encode_cue_point",
    "encode_loop_region",
    "encode_cue_array",
    "encode_loop_array",
    "patch_cue_points",
    "patch_loop_regions",
    "patch_rating",
    "patch_manual_bpm",
    "patch_key_signature_index",
    "patch_color_index",
    "patch_tags",
    "patch_start_point",
]
