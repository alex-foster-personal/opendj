"""Phase 4 SYNC-04 read-side: TSAF cue array parser tests.

These exercise the schema-v2 addendum encoding round-trip. We use the
djay_writer's encoders to build fixture blobs, parse them back with the
reader, and assert the shape survives. Byte-level fidelity is asserted
for the stable tokens (``0x2b``, ``0x08``, ``0x0b``, terminator ``0x00``).
"""
from __future__ import annotations

import struct

import pytest

from apps.shared.djay_db import (
    _tsaf_parse_cue_element,
    _tsaf_scan_array,
    parse_cues_from_blob,
)
from apps.shared.normalised import NormalisedCue
from apps.sync.djay_writer import (
    encode_cue_array,
    encode_cue_point,
    patch_cue_points,
    patch_loop_regions,
)

pytestmark = pytest.mark.requirement("SYNC-04")


def _wrap_in_tsaf_envelope(body: bytes, *, trail: bytes = b"\x0b\x05\x00\x00") -> bytes:
    """Return a minimal TSAF-shaped blob with a tail terminator."""
    header = b"TSAF" + b"\x00" * 12  # 16 byte header, matches _tsaf_kv skip
    return header + body + trail


def test_encode_cue_point_starts_with_class_marker():
    cue = NormalisedCue(position_msec=0, kind="memory")
    blob = encode_cue_point(cue)
    assert blob[:2] == b"\x2b\x08"
    assert b"ADCMediaItemCuePoint\x00" in blob
    assert blob.endswith(b"\x00")


def test_encode_cue_array_count_matches_elements():
    cues = [
        NormalisedCue(position_msec=1000, kind="memory"),
        NormalisedCue(position_msec=2000, kind="hot", index=0),
        NormalisedCue(position_msec=3000, kind="hot", index=1),
    ]
    arr = encode_cue_array(cues)
    assert arr[0] == 0x0B
    count = struct.unpack("<I", arr[1:5])[0]
    assert count == 3


def test_round_trip_single_memory_cue():
    cues_in = [NormalisedCue(position_msec=12345, kind="memory", name="intro")]
    blob = _wrap_in_tsaf_envelope(b"")
    blob = patch_cue_points(blob, cues_in)
    out = parse_cues_from_blob(blob)
    assert len(out) == 1
    assert out[0].position_msec == 12345
    assert out[0].kind == "memory"
    assert out[0].name == "intro"


def test_round_trip_multiple_cues():
    cues_in = [
        NormalisedCue(position_msec=0, kind="memory"),
        NormalisedCue(position_msec=1000, kind="hot", index=0, name="drop"),
        NormalisedCue(position_msec=2500, kind="hot", index=2,
                      color_rgb=(255, 0, 0)),
        NormalisedCue(position_msec=60000, kind="memory", name="end"),
    ]
    blob = _wrap_in_tsaf_envelope(b"")
    blob = patch_cue_points(blob, cues_in)
    out = parse_cues_from_blob(blob)
    assert len(out) == 4
    positions = sorted(c.position_msec for c in out)
    assert positions == [0, 1000, 2500, 60000]


def test_round_trip_preserves_hot_cue_index():
    cues_in = [
        NormalisedCue(position_msec=i * 1000, kind="hot", index=i)
        for i in range(8)
    ]
    blob = _wrap_in_tsaf_envelope(b"")
    blob = patch_cue_points(blob, cues_in)
    out = parse_cues_from_blob(blob)
    by_pos = {c.position_msec: c for c in out}
    for i in range(8):
        cue = by_pos[i * 1000]
        assert cue.kind == "hot"
        assert cue.index == i


def test_round_trip_preserves_color():
    cues_in = [
        NormalisedCue(position_msec=5000, kind="hot", index=0, color_rgb=(0, 255, 0)),
    ]
    blob = _wrap_in_tsaf_envelope(b"")
    blob = patch_cue_points(blob, cues_in)
    out = parse_cues_from_blob(blob)
    assert len(out) == 1
    assert out[0].color_rgb == (0, 255, 0)


def test_round_trip_preserves_utf8_name():
    cues_in = [NormalisedCue(position_msec=1000, kind="memory", name="café ☕")]
    blob = _wrap_in_tsaf_envelope(b"")
    blob = patch_cue_points(blob, cues_in)
    out = parse_cues_from_blob(blob)
    assert out[0].name == "café ☕"


def test_empty_cue_list_encodes_zero_count():
    blob = _wrap_in_tsaf_envelope(b"")
    blob = patch_cue_points(blob, [])
    assert b"\x08cuePoints\x00\x0b\x00\x00\x00\x00" in blob
    out = parse_cues_from_blob(blob)
    assert out == []


def test_absent_field_returns_empty_list():
    blob = _wrap_in_tsaf_envelope(b"")  # no cuePoints inserted
    out = parse_cues_from_blob(blob)
    assert out == []


def test_tsaf_scan_array_on_absent_field():
    blob = _wrap_in_tsaf_envelope(b"")
    assert _tsaf_scan_array(blob, b"cuePoints") is None


def test_tsaf_scan_array_counts_elements():
    cues_in = [
        NormalisedCue(position_msec=0, kind="memory"),
        NormalisedCue(position_msec=1, kind="memory"),
        NormalisedCue(position_msec=2, kind="memory"),
    ]
    blob = _wrap_in_tsaf_envelope(b"")
    blob = patch_cue_points(blob, cues_in)
    span = _tsaf_scan_array(blob, b"cuePoints")
    assert span is not None
    start, _end, count = span
    assert count == 3
    assert blob[start] == 0x0B


def test_loop_region_round_trip():
    loops_in = [
        NormalisedCue(position_msec=4000, kind="loop",
                      loop_length_msec=8000, name="loop A"),
    ]
    blob = _wrap_in_tsaf_envelope(b"")
    blob = patch_loop_regions(blob, loops_in)
    out = parse_cues_from_blob(blob)
    assert len(out) == 1
    assert out[0].kind == "loop"
    assert out[0].loop_length_msec == 8000
    assert out[0].name == "loop A"


def test_cues_and_loops_coexist():
    cues = [NormalisedCue(position_msec=1000, kind="hot", index=0)]
    loops = [NormalisedCue(position_msec=5000, kind="loop", loop_length_msec=2000)]
    blob = _wrap_in_tsaf_envelope(b"")
    blob = patch_cue_points(blob, cues)
    blob = patch_loop_regions(blob, loops)
    out = parse_cues_from_blob(blob)
    kinds = sorted(c.kind for c in out)
    assert kinds == ["hot", "loop"]


def test_sub_ms_positions_round_via_int():
    # 1.2345 seconds -> 1235 ms after rounding
    cues_in = [NormalisedCue(position_msec=1235, kind="memory")]
    blob = _wrap_in_tsaf_envelope(b"")
    blob = patch_cue_points(blob, cues_in)
    out = parse_cues_from_blob(blob)
    # Float32 precision: 1235 ms should survive within 1 ms (1.235s exact f32).
    assert abs(out[0].position_msec - 1235) <= 1


def test_patch_replace_existing_array():
    # Patch twice with different counts; reader sees the latest.
    blob = _wrap_in_tsaf_envelope(b"")
    blob = patch_cue_points(
        blob,
        [NormalisedCue(position_msec=0, kind="memory")],
    )
    blob = patch_cue_points(
        blob,
        [
            NormalisedCue(position_msec=1000, kind="memory"),
            NormalisedCue(position_msec=2000, kind="hot", index=0),
        ],
    )
    out = parse_cues_from_blob(blob)
    positions = sorted(c.position_msec for c in out)
    assert positions == [1000, 2000]


def test_parse_cue_element_returns_class():
    cue = NormalisedCue(position_msec=100, kind="memory")
    single = encode_cue_point(cue)
    parsed = _tsaf_parse_cue_element(single, 0)
    assert parsed is not None
    fields, _end = parsed
    assert fields["class"] == "ADCMediaItemCuePoint"


def test_remove_all_cues_via_empty_patch():
    blob = _wrap_in_tsaf_envelope(b"")
    blob = patch_cue_points(
        blob,
        [NormalisedCue(position_msec=100, kind="memory")],
    )
    assert len(parse_cues_from_blob(blob)) == 1
    blob = patch_cue_points(blob, [])
    assert parse_cues_from_blob(blob) == []
