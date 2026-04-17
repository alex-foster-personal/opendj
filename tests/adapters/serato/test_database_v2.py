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
