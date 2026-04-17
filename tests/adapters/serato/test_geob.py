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
