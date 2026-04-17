"""Phase 4 SYNC-04/05: djay TSAF preserve-then-patch byte-level tests."""
from __future__ import annotations

import struct

import pytest

from apps.shared.djay_db import (
    _tsaf_scan_array,
    extract_float32_before_field,
    extract_rating_from_tsaf,
    parse_cues_from_blob,
)
from apps.shared.normalised import NormalisedCue
from apps.sync.djay_writer import (
    encode_cue_array,
    encode_cue_point,
    encode_loop_region,
    patch_color_index,
    patch_cue_points,
    patch_key_signature_index,
    patch_loop_regions,
    patch_manual_bpm,
    patch_rating,
    patch_start_point,
    patch_tags,
)

pytestmark = pytest.mark.requirement("SYNC-04")


def _envelope(body: bytes = b"", trail: bytes = b"\x0b\x05\x00\x00") -> bytes:
    return b"TSAF" + b"\x00" * 12 + body + trail


def test_encode_cue_point_is_non_empty():
    cue = NormalisedCue(position_msec=1000, kind="memory")
    assert len(encode_cue_point(cue)) > 4


def test_patch_rating_replaces_existing_byte():
    blob = _envelope(b"\x0f\x03\x08rating\x00")
    new = patch_rating(blob, 5)
    assert extract_rating_from_tsaf(new) == 5


def test_patch_rating_inserts_when_absent():
    blob = _envelope()
    new = patch_rating(blob, 4)
    assert extract_rating_from_tsaf(new) == 4


def test_patch_manual_bpm_writes_float32_before_key():
    blob = _envelope()
    new = patch_manual_bpm(blob, 128.5)
    assert extract_float32_before_field(new, b"manualBPM") == pytest.approx(128.5)


def test_patch_manual_bpm_updates_existing_value():
    blob = _envelope(
        b"\x13\x00\x00\x00" + struct.pack("<f", 120.0) + b"\x08manualBPM\x00"
    )
    new = patch_manual_bpm(blob, 128.0)
    assert extract_float32_before_field(new, b"manualBPM") == pytest.approx(128.0)


def test_patch_key_signature_index_inserts_byte():
    blob = _envelope()
    new = patch_key_signature_index(blob, 5)
    needle = b"\x08keySignatureIndex\x00"
    assert needle in new
    p = new.find(needle)
    assert new[p - 1] == 5


def test_patch_color_index_inserts():
    blob = _envelope()
    new = patch_color_index(blob, 3)
    assert b"\x08colorIndex\x00" in new


def test_patch_tags_inserts_utf8_string():
    blob = _envelope()
    new = patch_tags(blob, "house, techno")
    assert b"\x08house, techno\x00\x08tags\x00" in new


def test_patch_start_point_float32():
    blob = _envelope()
    new = patch_start_point(blob, 5.25)
    assert extract_float32_before_field(new, b"startPoint") == pytest.approx(5.25)


def test_cue_array_round_trip_via_patch_then_parse():
    cues = [
        NormalisedCue(position_msec=0, kind="memory"),
        NormalisedCue(position_msec=1000, kind="hot", index=0, name="drop"),
    ]
    blob = patch_cue_points(_envelope(), cues)
    out = parse_cues_from_blob(blob)
    assert len(out) == 2


def test_cue_array_add_one_to_fixture():
    initial = [
        NormalisedCue(position_msec=1000, kind="memory"),
        NormalisedCue(position_msec=2000, kind="memory"),
        NormalisedCue(position_msec=3000, kind="memory"),
    ]
    blob = patch_cue_points(_envelope(), initial)
    initial.append(NormalisedCue(position_msec=4000, kind="memory"))
    blob = patch_cue_points(blob, initial)
    out = parse_cues_from_blob(blob)
    assert len(out) == 4
    assert {c.position_msec for c in out} == {1000, 2000, 3000, 4000}


def test_cue_array_remove_one_from_fixture():
    initial = [
        NormalisedCue(position_msec=1000, kind="memory"),
        NormalisedCue(position_msec=2000, kind="memory"),
        NormalisedCue(position_msec=3000, kind="memory"),
        NormalisedCue(position_msec=4000, kind="memory"),
    ]
    blob = patch_cue_points(_envelope(), initial)
    reduced = [c for c in initial if c.position_msec != 3000]
    blob = patch_cue_points(blob, reduced)
    out = parse_cues_from_blob(blob)
    assert len(out) == 3


def test_loop_regions_splice_round_trip():
    loops = [
        NormalisedCue(position_msec=10000, kind="loop", loop_length_msec=4000),
    ]
    blob = patch_loop_regions(_envelope(), loops)
    out = parse_cues_from_blob(blob)
    assert len(out) == 1 and out[0].kind == "loop"


def test_cued_to_empty_clears_array():
    blob = patch_cue_points(
        _envelope(), [NormalisedCue(position_msec=1000, kind="memory")]
    )
    assert _tsaf_scan_array(blob, b"cuePoints") is not None
    blob = patch_cue_points(blob, [])
    span = _tsaf_scan_array(blob, b"cuePoints")
    assert span is not None and span[2] == 0


def test_empty_to_cued_inserts_field():
    blob = _envelope()
    assert _tsaf_scan_array(blob, b"cuePoints") is None
    blob = patch_cue_points(blob, [NormalisedCue(position_msec=0, kind="memory")])
    span = _tsaf_scan_array(blob, b"cuePoints")
    assert span is not None and span[2] == 1


def test_encode_cue_array_zero_count():
    arr = encode_cue_array([])
    assert arr == b"\x0b\x00\x00\x00\x00"


def test_encode_loop_region_has_length_token():
    loop = NormalisedCue(position_msec=1000, kind="loop", loop_length_msec=2000)
    blob = encode_loop_region(loop)
    assert b"\x08length\x00" in blob


def test_patch_tags_replaces_existing_string():
    blob = _envelope(b"\x08old\x00\x08tags\x00")
    new = patch_tags(blob, "new")
    assert b"\x08new\x00\x08tags\x00" in new
    assert b"\x08old\x00\x08tags\x00" not in new
