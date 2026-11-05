"""database V2 + subcrate codec tests (OPEN-02c)."""

from __future__ import annotations

import pytest

from apps.adapters.serato.database_v2 import CrateTrack, DatabaseV2, Subcrate
from apps.adapters.serato.tagstream import RawTag


def _make_track(**overrides: str) -> CrateTrack:
    defaults = {
        "file_path": "Music/fixture/01.mp3",
        "title": "Nominal Title",
        "artist": "Alice",
        "album": "Fixture Vol 1",
        "bpm": "120.00",
        "key": "8A",
        "file_type": "mp3",
    }
    defaults.update(overrides)
    return CrateTrack(**defaults)


pytestmark = pytest.mark.requirement("OPEN-02")


@pytest.mark.requirement("OPEN-02c")
def test_empty_database_roundtrip(tmp_path) -> None:
    db = DatabaseV2()
    target = tmp_path / "database V2"
    db.write(target)
    readback = DatabaseV2.read(target)
    assert readback == db


@pytest.mark.requirement("OPEN-02c")
def test_single_track_roundtrip(tmp_path) -> None:
    db = DatabaseV2(tracks=(_make_track(),))
    encoded = db.to_bytes()
    readback = DatabaseV2.read(encoded)
    assert readback.tracks == db.tracks


@pytest.mark.requirement("OPEN-02c")
def test_unknown_leaf_tag_preserved() -> None:
    """Unknown tags inside otrk round-trip intact (open-dj §9)."""
    extra = RawTag(type="xRAT", payload=b"4")
    track = _make_track()
    track.extra_tags = (extra,)
    db = DatabaseV2(tracks=(track,))
    readback = DatabaseV2.read(db.to_bytes())
    assert readback.tracks[0].extra_tags == (extra,)


@pytest.mark.requirement("OPEN-02c")
def test_header_tags_roundtrip() -> None:
    header = (RawTag(type="vrsn", payload=b"\x00\x00"),)
    db = DatabaseV2(tracks=(_make_track(),), header_tags=header)
    readback = DatabaseV2.read(db.to_bytes())
    assert readback.header_tags == header
    assert readback.tracks == db.tracks


@pytest.mark.requirement("OPEN-02c")
def test_subcrate_roundtrip(tmp_path) -> None:
    crate = Subcrate(
        name="house",
        track_paths=("Music/fixture/01.mp3", "Music/fixture/02.mp3"),
    )
    target = tmp_path / "Subcrates" / "house.crate"
    crate.write(target)
    readback = Subcrate.read(target)
    assert readback.track_paths == crate.track_paths
    assert readback.name == "house"


@pytest.mark.requirement("OPEN-02c")
def test_subcrate_empty_roundtrip(tmp_path) -> None:
    crate = Subcrate(name="empty", track_paths=())
    target = tmp_path / "Subcrates" / "empty.crate"
    crate.write(target)
    readback = Subcrate.read(target)
    assert readback.track_paths == ()


@pytest.mark.requirement("OPEN-02c")
def test_two_consecutive_writes_produce_identical_bytes(tmp_path) -> None:
    db = DatabaseV2(tracks=(_make_track(title="One"), _make_track(title="Two")))
    a = tmp_path / "a"
    b = tmp_path / "b"
    db.write(a)
    db.write(b)
    assert a.read_bytes() == b.read_bytes()


# ---- Serato's own layout, built byte by byte from the format notes ----------
# Mixxx seratofeature.cpp (parseTrack, parseCrateTrackPath) and serato-tags:
# a 4-byte ASCII tag, a 4-byte big-endian length, then the payload; every
# string is UTF-16-BE; a crate wraps each track's ``ptrk`` in an ``otrk``.
# These bytes are written here by hand, not by this codec's writer, so a
# misreading the writer shares cannot pass.


def _tlv(tag: str, payload: bytes) -> bytes:
    return tag.encode("ascii") + len(payload).to_bytes(4, "big") + payload


def _u16(text: str) -> bytes:
    return text.encode("utf-16-be")


@pytest.mark.requirement("DJIMPORT-01")
def test_database_v2_as_serato_writes_it_gives_bpm_type_and_cjk_title() -> None:
    """[if] Serato's database V2 stores tbpm and ttyp as UTF-16-BE [then] the BPM, type and a CJK title read back, [else stop]."""
    track = _tlv(
        "otrk",
        _tlv("pfil", _u16("Music/Invented/a.mp3"))
        + _tlv("tsng", _u16("日本"))
        + _tlv("tbpm", _u16("128.00"))
        + _tlv("ttyp", _u16("mp3")),
    )
    db = DatabaseV2.read(_tlv("vrsn", _u16("2.0/Serato Scratch LIVE Database")) + track)
    (row,) = db.tracks
    assert (row.file_path, row.title, row.bpm, row.file_type) == (
        "Music/Invented/a.mp3",
        "日本",
        "128.00",
        "mp3",
    )
    # The legacy ASCII spelling this codec used to write still reads.
    legacy = DatabaseV2.read(_tlv("otrk", _tlv("pfil", _u16("a.mp3")) + _tlv("tbpm", b"90.5")))
    assert legacy.tracks[0].bpm == "90.5"


@pytest.mark.requirement("DJIMPORT-01")
def test_a_crate_as_serato_writes_it_lists_its_tracks_in_order(tmp_path) -> None:
    """[if] a Serato crate nests each ptrk inside an otrk [then] every path reads back in crate order, [else stop]."""
    crate = tmp_path / "Invented.crate"
    crate.write_bytes(
        _tlv("vrsn", _u16("1.0/Serato ScratchLive Crate"))
        + _tlv("otrk", _tlv("ptrk", _u16("Music/Invented/b.mp3")))
        + _tlv("otrk", _tlv("ptrk", _u16("Music/Invented/a.mp3")))
    )
    assert Subcrate.read(crate).track_paths == ("Music/Invented/b.mp3", "Music/Invented/a.mp3")
    # And what this codec writes now is that same nested layout.
    rewritten = tmp_path / "Rewritten.crate"
    Subcrate(name="Rewritten", track_paths=("x.mp3",)).write(rewritten)
    assert rewritten.read_bytes() == _tlv("otrk", _tlv("ptrk", _u16("x.mp3")))


@pytest.mark.requirement("DJIMPORT-01")
def test_a_crate_track_without_a_path_stops_the_read(tmp_path) -> None:
    """[if] a crate otrk holds no ptrk [then] the read stops instead of dropping the track, [else stop]."""
    crate = tmp_path / "Broken.crate"
    crate.write_bytes(_tlv("otrk", _tlv("xxxx", b"")))
    with pytest.raises(ValueError, match="no ptrk"):
        Subcrate.read(crate)
