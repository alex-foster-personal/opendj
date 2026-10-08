"""GEOB codec tests -- Markers2 + BeatGrid round-trip (OPEN-02c)."""

from __future__ import annotations

import pytest

from apps.adapters.serato.geob import (
    BeatGrid,
    BeatGridMarker,
    Markers2,
    Markers2Cue,
    Markers2Loop,
    encode_beatgrid,
    encode_markers2,
    parse_beatgrid,
    parse_markers2,
)

pytestmark = pytest.mark.requirement("OPEN-02")


@pytest.mark.requirement("OPEN-02c")
def test_markers2_empty_roundtrip() -> None:
    empty = Markers2()
    encoded = encode_markers2(empty)
    assert encoded[:2] == b"\x01\x01"
    decoded = parse_markers2(encoded)
    assert decoded == empty


@pytest.mark.requirement("OPEN-02c")
def test_markers2_single_cue_roundtrip() -> None:
    original = Markers2(
        cues=(
            Markers2Cue(index=0, position_ms=1234, color_rgb=0xCC0000, name="Intro"),
        ),
        track_color_rgb=0x00FF00,
    )
    encoded = encode_markers2(original)
    decoded = parse_markers2(encoded)
    assert decoded.cues == original.cues
    assert decoded.track_color_rgb == original.track_color_rgb
    # A second round-trip is byte-stable.
    assert encode_markers2(decoded) == encoded


@pytest.mark.requirement("OPEN-02c")
def test_markers2_loop_roundtrip() -> None:
    original = Markers2(
        loops=(
            Markers2Loop(
                index=2,
                start_ms=500,
                end_ms=4500,
                color_rgb=0x2222DD,
                name="A-section",
                locked=True,
            ),
        ),
        bpm_locked=True,
    )
    encoded = encode_markers2(original)
    decoded = parse_markers2(encoded)
    assert decoded.loops == original.loops
    assert decoded.bpm_locked is True


@pytest.mark.requirement("OPEN-02c")
def test_markers2_bad_version_preserves_raw() -> None:
    # Starts with 0x02 0x00 -- not a version we recognise.
    bogus = b"\x02\x00hello-world"
    decoded = parse_markers2(bogus)
    assert decoded.unknown_tags == (("__raw__", bogus),)
    # Re-encoding yields the original verbatim.
    assert encode_markers2(decoded) == bogus


@pytest.mark.requirement("OPEN-02c")
def test_markers2_raw_plus_cue_and_loop_does_not_drop_structured() -> None:
    """Regression: a ``__raw__`` unknown_tag must not short-circuit encoding
    when the Markers2 also carries cues / loops. Before the fix, the early
    ``return raw`` in encode_markers2 silently dropped every cue and loop
    any time a __raw__ was present alongside structured content.
    """
    bogus_raw = b"\x02\x00passthrough"
    original = Markers2(
        cues=(
            Markers2Cue(
                index=0, position_ms=1000, color_rgb=0xCC0000, name="Drop"
            ),
        ),
        loops=(
            Markers2Loop(
                index=1,
                start_ms=2000,
                end_ms=4000,
                color_rgb=0x2222DD,
                name="L1",
                locked=False,
            ),
        ),
        unknown_tags=(("__raw__", bogus_raw),),
    )
    encoded = encode_markers2(original)
    # The encoded payload is a valid Markers2 frame (version 0x01 0x01 +
    # base64), NOT the verbatim ``__raw__`` blob. The raw blob is dropped
    # because it cannot be spliced into a tag stream.
    assert encoded[:2] == b"\x01\x01"
    assert encoded != bogus_raw
    decoded = parse_markers2(encoded)
    assert decoded.cues == original.cues
    assert decoded.loops == original.loops


@pytest.mark.requirement("OPEN-02c")
def test_markers2_raw_alone_still_roundtrips_verbatim() -> None:
    """Pure-passthrough case must still return the raw blob verbatim."""
    bogus = b"\x02\x00verbatim-me"
    original = Markers2(unknown_tags=(("__raw__", bogus),))
    assert encode_markers2(original) == bogus


@pytest.mark.requirement("OPEN-02c")
def test_beatgrid_constant_tempo_roundtrip() -> None:
    grid = BeatGrid(markers=(BeatGridMarker(position_seconds=0.0, bpm=128.0),))
    encoded = encode_beatgrid(grid)
    decoded = parse_beatgrid(encoded)
    assert decoded == grid


@pytest.mark.requirement("OPEN-02c")
def test_beatgrid_variable_tempo_roundtrip() -> None:
    grid = BeatGrid(
        markers=(
            BeatGridMarker(position_seconds=0.0, beats_till_next=16),
            BeatGridMarker(position_seconds=8.0, beats_till_next=32),
            BeatGridMarker(position_seconds=24.0, bpm=126.5),
        ),
        footer=b"\x00",
    )
    encoded = encode_beatgrid(grid)
    decoded = parse_beatgrid(encoded)
    assert decoded == grid


# ---- Markers2 as Serato writes it, built byte by byte ----------------------
# serato-tags docs/serato_markers2.md and Mixxx markers2.cpp: the frame is
# 01 01 + base64 (no '=' padding, a linefeed every 72 chars) + NUL padding to
# 470 bytes; the decoded body repeats 01 01, then NUL-terminated entry names
# with a uint32 length, then one NUL. CUE: 00, index, uint32 ms, 00, RGB,
# 00 00, UTF-8 name. LOOP: 00, index, start, end, ff ff ff ff, 00, RGB, 00,
# locked, UTF-8 name. Built here by hand, not by encode_markers2.


def _entry(name: str, payload: bytes) -> bytes:
    return name.encode("ascii") + b"\x00" + len(payload).to_bytes(4, "big") + payload


def _serato_frame(body: bytes, *, extra_char: bool = False) -> bytes:
    import base64

    text = base64.b64encode(b"\x01\x01" + body + b"\x00").rstrip(b"=")
    if extra_char:
        # serato-tags: Serato may write one character past a multiple of 4.
        text += b"A" * ((1 - len(text)) % 4)
    lines = b"\n".join(text[i : i + 72] for i in range(0, len(text), 72))
    return (b"\x01\x01" + lines).ljust(470, b"\x00")


_CUE = _entry(
    "CUE",
    b"\x00\x02" + (61_250).to_bytes(4, "big") + b"\x00" + b"\xcc\x88\x00" + b"\x00\x00"
    + "Drop é".encode() + b"\x00",
)
_LOOP = _entry(
    "LOOP",
    b"\x00\x01" + (1_000).to_bytes(4, "big") + (3_086).to_bytes(4, "big") + b"\xff" * 4
    + b"\x00" + b"\x27\xaa\xe1" + b"\x00" + b"\x01" + b"Build\x00",
)
_COLOR = _entry("COLOR", b"\x00\x99\xff\x99")


@pytest.mark.requirement("DJIMPORT-01")
@pytest.mark.parametrize("extra_char", [False, True])
def test_markers2_as_serato_writes_it_gives_cues_loops_and_color(extra_char: bool) -> None:
    """[if] a Markers2 frame is laid out the way Serato writes it [then] every cue, loop, color and name reads back, [else stop]."""
    frame = _serato_frame(_COLOR + _CUE + _LOOP, extra_char=extra_char)
    parsed = parse_markers2(frame)
    assert parsed.cues == (
        Markers2Cue(index=2, position_ms=61_250, color_rgb=0xCC8800, name="Drop é", kind="hot"),
    )
    assert parsed.loops == (
        Markers2Loop(index=1, start_ms=1_000, end_ms=3_086, color_rgb=0x27AAE1, name="Build", locked=True),
    )
    assert parsed.track_color_rgb == 0x99FF99
    assert parsed.unknown_tags == ()


@pytest.mark.requirement("DJIMPORT-01")
def test_markers2_written_by_this_codec_is_laid_out_as_serato_writes_it() -> None:
    """[if] this codec writes a cue [then] the bytes match the hand-built Serato frame, [else stop]."""
    cue = Markers2Cue(index=2, position_ms=61_250, color_rgb=0xCC8800, name="Drop é", kind="hot")
    assert encode_markers2(Markers2(cues=(cue,))) == _serato_frame(_CUE)
