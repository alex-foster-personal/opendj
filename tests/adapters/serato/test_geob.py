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


@pytest.mark.requirement("DJIMPORT-6039")
def test_an_unreadable_id3_tag_raises_by_default_and_is_empty_only_when_not_strict(tmp_path) -> None:
    """[if] an MP3's ID3 tag does not parse [then] read_geob_frames raises by default, and returns an empty bundle
    only with strict=False, [else stop]."""
    from apps.adapters.serato.geob import read_geob_frames
    from apps.shared.id3v2 import Id3Error

    broken = tmp_path / "Invented Broken.mp3"
    broken.write_bytes(b"ID3\x03\x00\x00\x00\x00\x20\x00" + b"\x00" * 16)
    assert read_geob_frames(broken, strict=False).opaque_frames == {}
    with pytest.raises(Id3Error):
        read_geob_frames(broken)
    frameless = tmp_path / "Invented Frameless.mp3"
    frameless.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x0a" + b"\x00" * 10 + b"\xff\xfb\x90\x00" * 4)
    assert read_geob_frames(frameless, strict=False).opaque_frames == {}
    with pytest.raises(Id3Error, match="no frames"):
        read_geob_frames(frameless)  # a tag that holds no frame is a counted loss, not a clean tag
    untagged = tmp_path / "Invented Clean.mp3"
    untagged.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 16)
    assert read_geob_frames(untagged).opaque_frames == {}  # no tag is not an error


def test_bytes_after_the_markers2_entry_list_terminator_make_it_undecodable() -> None:
    """[if] a decoded Markers2 body has bytes after its entry-list NUL [then] it is undecodable, not clean, [else stop]."""
    import base64
    import struct

    from apps.adapters.serato.geob import parse_markers2

    cue = b"CUE\x00" + struct.pack(">I", 13) + b"\x00" * 13  # a complete, valid CUE entry
    body = b"\x01\x01\x00" + cue  # the entry list ends at the NUL, and a whole CUE follows it
    payload = b"\x01\x01" + base64.b64encode(body)
    assert [name for name, _ in parse_markers2(payload).unknown_tags] == ["__raw__"]


@pytest.mark.parametrize("name_entry", ["CUE", "LOOP"])
@pytest.mark.parametrize("name", [b"Bad\xff\x00", b"Bad", b"Bad\x00HIDDEN"], ids=["not-utf8", "no-nul", "bytes-after-nul"])
def test_a_markers2_name_that_is_not_utf8_or_unterminated_makes_the_frame_undecodable(name_entry: str, name: bytes) -> None:
    """[if] a CUE or LOOP name is not valid UTF-8 or has no NUL terminator [then] the frame is undecodable, not read as complete, [else stop]."""
    from apps.adapters.serato.geob import parse_markers2

    if name_entry == "CUE":
        payload = b"\x00\x02" + (61_250).to_bytes(4, "big") + b"\x00" + b"\xcc\x88\x00" + b"\x00\x00" + name
    else:
        payload = (
            b"\x00\x01" + (1_000).to_bytes(4, "big") + (3_086).to_bytes(4, "big") + b"\xff" * 4
            + b"\x00" + b"\x27\xaa\xe1" + b"\x00" + b"\x01" + name
        )
    decoded = parse_markers2(_serato_frame(_entry(name_entry, payload)))
    assert [tag for tag, _ in decoded.unknown_tags] == ["__raw__"]
    assert not decoded.cues and not decoded.loops


def test_non_nul_bytes_after_the_markers2_text_make_it_undecodable() -> None:
    """[if] a Markers2 frame has non-NUL bytes after its base64 text [then] it is read as undecodable, [else stop]."""
    import base64

    from apps.adapters.serato.geob import parse_markers2

    payload = b"\x01\x01" + base64.b64encode(b"\x01\x01\x00") + b"\x00CORRUPT"  # a valid empty entry list, then junk
    assert [name for name, _ in parse_markers2(payload).unknown_tags] == ["__raw__"]


def test_a_markers2_body_without_its_inner_header_is_undecodable() -> None:
    """[if] a Markers2 frame's decoded body lacks its inner 01 01 header [then] it is read as undecodable, not as an entry list, [else stop]."""
    import base64

    from apps.adapters.serato.geob import parse_markers2

    payload = b"\x01\x01" + base64.b64encode(b"\x00")  # only the entry-list NUL, no inner header
    assert [name for name, _ in parse_markers2(payload).unknown_tags] == ["__raw__"]



@pytest.mark.requirement("DJIMPORT-6039")
def test_a_markers2_loop_whose_end_is_not_after_its_start_is_counted_as_lost() -> None:
    """[if] a Markers2 LOOP ends at or before its start [then] it is not kept as a zero-length loop and is counted
    as backwards, and a loop that runs forward reads as before, [else stop]."""
    backwards = _entry(
        "LOOP",
        b"\x00\x01" + (3_086).to_bytes(4, "big") + (1_000).to_bytes(4, "big") + b"\xff" * 4
        + b"\x00" + b"\x27\xaa\xe1" + b"\x00" + b"\x01" + b"Build\x00",
    )
    control = parse_markers2(_serato_frame(_COLOR + _LOOP))
    assert control.backwards_loops == 0  # control: the forward loop reads
    assert len(control.loops) == 1
    parsed = parse_markers2(_serato_frame(_COLOR + backwards))
    assert parsed.loops == ()
    assert parsed.backwards_loops == 1


# ---- Serato Markers_ (legacy, ID3 layout) built byte by byte ---------------
# serato-tags docs/serato_markers_.md: 02 05, uint32 count 14, then 14 entries
# of 22 bytes (5 cues then 9 loops), then a 4-byte track color. An entry is
# set flag, serato32 start, set flag, serato32 end, 00 7f 7f 7f 7f 7f,
# serato32 RGB, type (1 cue, 3 loop), locked. 7f flags mean "not set".


def _s32(value: int) -> bytes:
    """serato-tags' documented encoder, kept apart from the codec's decoder."""
    a, b, c = (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF
    z = c & 0x7F
    y = ((c >> 7) | (b << 1)) & 0x7F
    x = ((b >> 6) | (a << 2)) & 0x7F
    w = a >> 5
    return bytes((w, x, y, z))


def _legacy_entry(kind: int, start: int | None = None, end: int | None = None, rgb: int = 0, locked: bool = False) -> bytes:
    start_bytes = b"\x00" + _s32(start) if start is not None else b"\x7f" * 5
    end_bytes = b"\x00" + _s32(end) if end is not None else b"\x7f" * 5
    return start_bytes + end_bytes + b"\x00\x7f\x7f\x7f\x7f\x7f" + _s32(rgb) + bytes((kind, int(locked)))


def _legacy_frame(cues: dict[int, tuple[int, int]], loops: dict[int, tuple[int, int, bool]]) -> bytes:
    entries = [
        _legacy_entry(1, cues[i][0], rgb=cues[i][1]) if i in cues else _legacy_entry(1)
        for i in range(5)
    ] + [
        _legacy_entry(3, loops[i][0], loops[i][1], locked=loops[i][2]) if i in loops else _legacy_entry(3)
        for i in range(9)
    ]
    return b"\x02\x05" + (14).to_bytes(4, "big") + b"".join(entries) + b"\x07\x7f\x7f\x7f"


@pytest.mark.requirement("DJIMPORT-01")
def test_serato32_matches_the_documented_examples() -> None:
    """[if] serato-tags' documented serato32 bytes are decoded [then] they give its documented values, [else stop]."""
    from apps.adapters.serato.geob import _serato32

    assert _serato32(bytes.fromhex("06300000")) == 0xCC0000
    assert _serato32(bytes.fromhex("077f7f7f")) == 0xFFFFFF
    assert _serato32(_s32(218_456)) == 218_456


@pytest.mark.requirement("DJIMPORT-01")
def test_markers_legacy_wins_and_a_cue_it_clears_is_not_imported() -> None:
    """[if] Markers_ moves cue 0 and leaves cue 1 unset [then] cue 0 moves keeping its name, cue 1 is dropped, cue 6 stays, [else stop]."""
    from apps.adapters.serato.geob import overlay_markers_legacy, parse_markers_legacy

    markers2 = Markers2(
        cues=(
            Markers2Cue(index=0, position_ms=1_000, color_rgb=0x0000CC, name="Intro", kind="hot"),
            Markers2Cue(index=1, position_ms=2_000, color_rgb=0xCC0000, name="Cleared", kind="hot"),
            Markers2Cue(index=6, position_ms=6_000, color_rgb=0x00CC00, name="Past five", kind="hot"),
        ),
        loops=(Markers2Loop(index=0, start_ms=0, end_ms=500, color_rgb=0x27AAE1, name="Roll", locked=False),),
    )
    legacy = parse_markers_legacy(_legacy_frame({0: (1_500, 0xCC8800)}, {0: (100, 2_186, True)}))

    merged = overlay_markers_legacy(markers2, legacy)

    assert [(c.index, c.position_ms, c.color_rgb, c.name) for c in merged.cues] == [
        (0, 1_500, 0xCC8800, "Intro"),
        (6, 6_000, 0x00CC00, "Past five"),
    ]
    assert [(lo.index, lo.start_ms, lo.end_ms, lo.locked, lo.name) for lo in merged.loops] == [
        (0, 100, 2_186, True, "Roll"),
    ]
    # Control: with no readable Markers_, Markers2 stands unchanged.
    assert overlay_markers_legacy(markers2, None) == markers2
    assert parse_markers_legacy(b"\x02\x05" + b"\x00" * 10) is None


@pytest.mark.requirement("DJIMPORT-01")
def test_markers_legacy_clears_unset_loop_slots_and_leaves_memory_cues_alone() -> None:
    """[if] Markers_ leaves loop 1 unset and Markers2 has a memory cue at index 2 [then] loop 1 goes, loop 9 and the memory cue stay, [else stop]."""
    from apps.adapters.serato.geob import overlay_markers_legacy, parse_markers_legacy

    markers2 = Markers2(
        cues=(Markers2Cue(index=2, position_ms=3_000, color_rgb=0, name="Memory", kind="memory"),),
        loops=(
            Markers2Loop(index=0, start_ms=0, end_ms=500, color_rgb=0, name="Kept", locked=False),
            Markers2Loop(index=1, start_ms=900, end_ms=1_400, color_rgb=0, name="Cleared", locked=False),
            Markers2Loop(index=9, start_ms=5_000, end_ms=5_500, color_rgb=0, name="Past nine", locked=False),
        ),
    )
    legacy = parse_markers_legacy(_legacy_frame({}, {0: (100, 600, False)}))

    merged = overlay_markers_legacy(markers2, legacy)

    assert [(c.index, c.kind, c.name) for c in merged.cues] == [(2, "memory", "Memory")]
    assert [(lo.index, lo.start_ms, lo.name) for lo in merged.loops] == [(0, 100, "Kept"), (9, 5_000, "Past nine")]


@pytest.mark.requirement("DJIMPORT-01")
def test_markers_legacy_end_at_zero_loop_reads_as_unset() -> None:
    """[if] Markers_ sets loop 0 to end at 0 ms [then] it reads as unset and does not overwrite the Markers2 loop there, [else stop]."""
    from apps.adapters.serato.geob import overlay_markers_legacy, parse_markers_legacy

    markers2 = Markers2(
        cues=(),
        loops=(Markers2Loop(index=0, start_ms=900, end_ms=1_400, color_rgb=0, name="Valid", locked=False),),
    )
    legacy = parse_markers_legacy(_legacy_frame({}, {0: (100, 0, False)}))

    assert legacy is not None and legacy.loops == {}
    assert overlay_markers_legacy(markers2, legacy).loops == ()

def _tamper(frame: bytes, entry: int, offset: int, value: int) -> bytes:
    at = 6 + entry * 22 + offset
    return frame[:at] + bytes((value,)) + frame[at + 1 :]


@pytest.mark.requirement("DJIMPORT-01")
def test_a_markers_legacy_frame_off_the_documented_layout_is_ignored() -> None:
    """[if] Markers_ has extra bytes, entries out of order or a bad set flag [then] it is ignored whole, [else stop]."""
    from apps.adapters.serato.geob import parse_markers_legacy

    good = _legacy_frame({0: (1_500, 0xCC8800)}, {0: (100, 600, False)})
    assert parse_markers_legacy(good) is not None  # control: the documented frame reads
    assert parse_markers_legacy(good + b"\x00") is None  # trailing data
    assert parse_markers_legacy(_tamper(good, 0, 20, 3)) is None  # a loop in a cue slot
    assert parse_markers_legacy(_tamper(good, 5, 20, 1)) is None  # a cue in a loop slot
    assert parse_markers_legacy(_tamper(good, 1, 0, 0x01)) is None  # set flag neither set nor unset
    assert parse_markers_legacy(_tamper(good, 0, 20, 0)) is None  # a SET entry must say its type
    loop_start_only = _tamper(_legacy_frame({}, {}), 5, 0, 0x00)  # loop 0: start set, end unset
    assert parse_markers_legacy(loop_start_only) is None
    loop_end_only = _tamper(_legacy_frame({}, {}), 6, 5, 0x00)  # loop 1: end set, start unset
    assert parse_markers_legacy(loop_end_only) is None
    cue_end_only = _tamper(_legacy_frame({}, {}), 1, 5, 0x00)  # cue 1: end set, start unset
    assert parse_markers_legacy(cue_end_only) is None
    cue_both_ends = _tamper(good, 0, 5, 0x00)  # cue 0: start set, end set too
    assert parse_markers_legacy(cue_both_ends) is None
    assert parse_markers_legacy(_tamper(good, 0, 1, 0x08)) is None  # set start past 24 bits
    assert parse_markers_legacy(_tamper(good, 0, 3, 0x80)) is None  # serato32 byte with its high bit set
    assert parse_markers_legacy(_tamper(good, 5, 6, 0x08)) is None  # set loop end past 24 bits
    assert parse_markers_legacy(_tamper(good, 2, 2, 0x80)) is None  # unset filler with a high bit
    assert parse_markers_legacy(_tamper(good, 2, 2, 0x10)) is None  # unset start not the 7f filler
    assert parse_markers_legacy(_tamper(good, 0, 8, 0x10)) is None  # cue's unset end not the 7f filler
    assert parse_markers_legacy(_tamper(good, 0, 10, 0x01)) is None  # reserved run, first byte
    assert parse_markers_legacy(_tamper(good, 7, 15, 0x00)) is None  # reserved run, last byte
    assert parse_markers_legacy(_tamper(good, 0, 16, 0x08)) is None  # cue color past 24 bits
    assert parse_markers_legacy(_tamper(good, 0, 18, 0x80)) is None  # cue color byte with its high bit set
    assert parse_markers_legacy(good[:-1] + b"\x80") is None  # track color byte with its high bit set
    assert parse_markers_legacy(_tamper(good, 5, 21, 0x02)) is None  # lock neither 0 nor 1
    assert parse_markers_legacy(good[:-4] + b"\x08\x00\x00\x00") is None  # track color past 24 bits
    # Controls: the largest valid values and a locked loop still read.
    edges = _legacy_frame({0: (0xFFFFFF, 0xFFFFFF)}, {0: (0, 0xFFFFFF, True)})
    parsed_edges = parse_markers_legacy(edges)
    assert parsed_edges is not None
    assert parsed_edges.cues == {0: (0xFFFFFF, 0xFFFFFF)} and parsed_edges.loops == {0: (0, 0xFFFFFF, True)}
    # An UNSET cue entry carrying type 0 is still the documented layout.
    unset_type0 = parse_markers_legacy(_tamper(good, 2, 20, 0))
    assert unset_type0 is not None and unset_type0.cues == {0: (1_500, 0xCC8800)}
    # An UNSET loop entry keeps loop type 3, so type 0 there is off the layout.
    assert parse_markers_legacy(_tamper(good, 7, 20, 0)) is None


@pytest.mark.requirement("DJIMPORT-01")
def test_a_locked_cue_entry_makes_markers_legacy_unreadable() -> None:
    """[if] a cue entry in Markers_ has its lock byte set [then] the frame is ignored whole, and a locked loop still reads, [else stop]."""
    from apps.adapters.serato.geob import parse_markers_legacy

    good = _legacy_frame({0: (1_500, 0xCC8800)}, {0: (100, 600, True)})
    assert parse_markers_legacy(good) is not None  # control: a locked LOOP is fine
    assert parse_markers_legacy(_tamper(good, 0, 21, 0x01)) is None  # set cue, locked
    assert parse_markers_legacy(_tamper(good, 4, 21, 0x01)) is None  # unset cue 4, locked


@pytest.mark.requirement("DJIMPORT-01")
def test_a_black_cue_at_zero_in_markers_legacy_reads_as_unset() -> None:
    """[if] Markers_ sets a cue at 0 ms with color 0
    [then] that slot reads as unset, while 0 ms with a color and a black cue later still read, [else stop]."""
    from apps.adapters.serato.geob import overlay_markers_legacy, parse_markers_legacy

    legacy = parse_markers_legacy(_legacy_frame({0: (0, 0), 1: (0, 0xCC0000), 2: (2_000, 0)}, {}))
    assert legacy is not None
    assert legacy.cues == {1: (0, 0xCC0000), 2: (2_000, 0)}
    markers2 = Markers2(
        cues=(Markers2Cue(index=0, position_ms=0, color_rgb=0, name="Bogus", kind="hot"),),
        loops=(),
    )
    assert [c.index for c in overlay_markers_legacy(markers2, legacy).cues] == [1, 2]
