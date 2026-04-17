"""End-to-end TraktorAdapter tests (OPEN-02d)."""

from __future__ import annotations

import pytest

from apps.open_dj import (
    Adapter,
    CuePoint,
    OpenDjLibrary,
    Playlist,
    Track,
    serialize_jcs,
)
from apps.adapters.traktor import TraktorAdapter, TraktorAdapterOptions


def _sample_library() -> OpenDjLibrary:
    t1 = Track(
        track_id="ignored",
        file_path="/Music/fixture/01.mp3",
        title="Sample One",
        artists=("Alice",),
        album="Fixture Vol 1",
        bpm=128.0,
        key_camelot="8A",
        rating=4,
        duration_ms=242000,
        cues=(
            CuePoint(index=0, position_ms=0, type="hot", name="Intro"),
            CuePoint(index=1, position_ms=30000, type="loop", name="Loop A", length_ms=4000),
        ),
    )
    t2 = Track(
        track_id="ignored2",
        file_path="/Music/fixture/02.mp3",
        title="Sample Two",
        artists=("Bob", "Charlie"),
        bpm=124.5,
        rating=None,
    )
    return OpenDjLibrary(
        version="0.1",
        tracks=(t1, t2),
        playlists=(Playlist(name="warmup", track_ids=()),),
    )


@pytest.mark.requirement("OPEN-02d")
def test_protocol_runtime_check() -> None:
    assert isinstance(TraktorAdapter(), Adapter)


@pytest.mark.requirement("OPEN-02d")
def test_capabilities_includes_rating_lossless() -> None:
    caps = TraktorAdapter().capabilities()
    assert caps.adapter == "traktor"
    support = {f.field: f.supports for f in caps.fields}
    assert support["rating"] == "lossless"
    assert support["cue_points.color"] == "lossy"


@pytest.mark.requirement("OPEN-02d")
def test_write_then_read_roundtrip(tmp_path) -> None:
    adapter = TraktorAdapter()
    lib = _sample_library()
    target = tmp_path / "collection.nml"
    write_report = adapter.write(lib, target)
    assert write_report.counts["tracks_written"] == 2

    lib_back, read_report = adapter.read(target)
    assert read_report.counts["tracks_read"] == 2
    by_title = {t.title: t for t in lib_back.tracks}
    assert "Sample One" in by_title
    t1 = by_title["Sample One"]
    assert t1.bpm == pytest.approx(128.0)
    assert t1.key_camelot == "8A"
    assert t1.rating == 4
    assert t1.duration_ms == 242000
    assert len(t1.cues) == 2
    assert t1.cues[0].type == "hot"
    assert t1.cues[1].type == "loop"
    assert t1.cues[1].length_ms == 4000


@pytest.mark.requirement("OPEN-02d")
def test_jcs_stable_across_two_writes(tmp_path) -> None:
    """Round-tripping the same library via NML -> open-dj is byte-stable."""
    adapter = TraktorAdapter()
    lib = _sample_library()
    a = tmp_path / "a.nml"
    b = tmp_path / "b.nml"
    adapter.write(lib, a)
    adapter.write(lib, b)
    assert a.read_bytes() == b.read_bytes()
    lib_a, _ = adapter.read(a)
    lib_b, _ = adapter.read(b)
    assert serialize_jcs(lib_a) == serialize_jcs(lib_b)


@pytest.mark.requirement("OPEN-02d")
def test_cue_color_not_matching_palette_emits_warning(tmp_path) -> None:
    adapter = TraktorAdapter()
    t = Track(
        track_id="t",
        file_path="/Music/fixture/03.mp3",
        title="Custom Colour",
        artists=("X",),
        cues=(
            CuePoint(index=0, position_ms=0, type="hot", color_rgb=0xFF00FF),
        ),
    )
    lib = OpenDjLibrary(version="0.1", tracks=(t,))
    report = adapter.write(lib, tmp_path / "collection.nml")
    colour_warns = [w for w in report.warnings if w.field == "cue_points.color"]
    assert len(colour_warns) == 1
    assert colour_warns[0].action == "downgraded"


@pytest.mark.requirement("OPEN-02d")
def test_extended_data_roundtrip(tmp_path) -> None:
    adapter = TraktorAdapter()
    t = Track(
        track_id="t",
        file_path="/Music/fixture/04.mp3",
        title="Ext Data",
        artists=("Y",),
        extensions={"x_traktor_extended_data": "SGVsbG8="},
    )
    lib = OpenDjLibrary(version="0.1", tracks=(t,))
    target = tmp_path / "collection.nml"
    adapter.write(lib, target)
    lib_back, _ = adapter.read(target)
    assert lib_back.tracks[0].extensions["x_traktor_extended_data"] == "SGVsbG8="


@pytest.mark.requirement("OPEN-02d")
def test_preserve_extended_data_false_drops(tmp_path) -> None:
    # Manually construct a collection.nml with an <EXTENDEDDATA> child.
    raw = (
        b"<?xml version='1.0' encoding='utf-8'?>\n"
        b"<NML VERSION=\"20\">\n"
        b"  <HEAD></HEAD><MUSICFOLDERS></MUSICFOLDERS>\n"
        b"  <COLLECTION ENTRIES=\"1\">\n"
        b"    <ENTRY TITLE=\"t\" ARTIST=\"x\">\n"
        b"      <LOCATION DIR=\"/:Music/:\" FILE=\"e.mp3\" VOLUME=\"\"></LOCATION>\n"
        b"      <EXTENDEDDATA>base64stuff</EXTENDEDDATA>\n"
        b"    </ENTRY>\n"
        b"  </COLLECTION>\n"
        b"  <PLAYLISTS></PLAYLISTS>\n"
        b"</NML>\n"
    )
    target = tmp_path / "collection.nml"
    target.write_bytes(raw)
    adapter = TraktorAdapter(options=TraktorAdapterOptions(preserve_extended_data=False))
    _, report = adapter.read(target)
    assert any(w.field == "extended_data" and w.action == "dropped" for w in report.warnings)


@pytest.mark.requirement("OPEN-02d")
def test_playlists_roundtrip(tmp_path) -> None:
    adapter = TraktorAdapter()
    t = Track(
        track_id="t",
        file_path="/Music/fixture/05.mp3",
        title="In Playlist",
        artists=("Z",),
    )
    pl = Playlist(name="set-a", track_ids=(t.track_id,))
    lib = OpenDjLibrary(version="0.1", tracks=(t,), playlists=(pl,))
    target = tmp_path / "collection.nml"
    adapter.write(lib, target)
    lib_back, _ = adapter.read(target)
    assert len(lib_back.playlists) == 1
    assert lib_back.playlists[0].name == "set-a"
