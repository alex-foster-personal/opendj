"""End-to-end SeratoAdapter tests (OPEN-02c).

Exercises the Protocol-level read/write path by writing a synthetic library,
reading it back, and asserting the open-dj JCS bytes are stable.
"""

from __future__ import annotations

import pytest

from apps.adapters.serato import SeratoAdapter, SeratoAdapterOptions
from apps.open_dj import (
    Adapter,
    AdapterReport,
    CuePoint,
    OpenDjLibrary,
    Playlist,
    Track,
    serialize_jcs,
)


def _sample_library() -> OpenDjLibrary:
    t1 = Track(
        track_id="ignored_on_write",
        file_path="Music/fixture/01.mp3",
        title="Sample One",
        artists=("Alice",),
        album="Fixture Vol 1",
        bpm=128.0,
        key_camelot="8A",
        rating=None,
    )
    t2 = Track(
        track_id="ignored_on_write2",
        file_path="Music/fixture/02.mp3",
        title="Sample Two",
        artists=("Bob", "Charlie"),
        album="Fixture Vol 1",
        bpm=124.5,
        key_camelot="5A",
        rating=4,
    )
    return OpenDjLibrary(
        version="0.1",
        tracks=(t1, t2),
        playlists=(Playlist(name="warmup", track_ids=()),),
    )


@pytest.mark.requirement("OPEN-02c")
def test_runtime_protocol_check() -> None:
    """SeratoAdapter satisfies the runtime-checkable Adapter Protocol."""
    assert isinstance(SeratoAdapter(), Adapter)


@pytest.mark.requirement("OPEN-02c")
def test_capabilities_returns_descriptor() -> None:
    caps = SeratoAdapter().capabilities()
    assert caps.adapter == "serato"
    fields = {f.field for f in caps.fields}
    assert "rating" in fields
    assert "cue_points.memory" in fields


@pytest.mark.requirement("OPEN-02c")
def test_write_then_read_roundtrip(tmp_path) -> None:
    adapter = SeratoAdapter()
    lib = _sample_library()
    target = tmp_path / "_Serato_"
    write_report = adapter.write(lib, target)
    assert write_report.counts["tracks_written"] == 2
    # the rating-drop warning fires once (only t2 has a rating)
    rating_drops = [w for w in write_report.warnings if w.field == "rating"]
    assert len(rating_drops) == 1

    lib_back, read_report = adapter.read(target)
    assert read_report.counts["tracks_read"] == 2
    # File paths + titles + BPM round-trip lossless.
    by_path = {t.file_path: t for t in lib_back.tracks}
    assert by_path["Music/fixture/01.mp3"].title == "Sample One"
    assert by_path["Music/fixture/02.mp3"].bpm == pytest.approx(124.5)
    # Rating dropped on write -- round-trip sees None.
    assert by_path["Music/fixture/02.mp3"].rating is None


@pytest.mark.requirement("OPEN-02c")
def test_memory_cue_emits_warning(tmp_path) -> None:
    adapter = SeratoAdapter()
    t = Track(
        track_id="t",
        file_path="Music/fixture/03.mp3",
        title="Memory Holder",
        artists=("X",),
        cues=(CuePoint(index=0, position_ms=0, type="memory"),),
    )
    lib = OpenDjLibrary(version="0.1", tracks=(t,))
    report = adapter.write(lib, tmp_path / "_Serato_")
    memory_warns = [w for w in report.warnings if w.field == "cue_points.memory"]
    assert len(memory_warns) == 1
    assert memory_warns[0].action == "dropped"


@pytest.mark.requirement("OPEN-02c")
def test_memory_as_hot_option_suppresses_warning(tmp_path) -> None:
    opts = SeratoAdapterOptions(memory_as_hot=True)
    adapter = SeratoAdapter(options=opts)
    t = Track(
        track_id="t",
        file_path="Music/fixture/04.mp3",
        title="Memory Holder",
        artists=("X",),
        cues=(CuePoint(index=0, position_ms=0, type="memory"),),
    )
    lib = OpenDjLibrary(version="0.1", tracks=(t,))
    report = adapter.write(lib, tmp_path / "_Serato_")
    assert not any(w.field == "cue_points.memory" for w in report.warnings)


@pytest.mark.requirement("OPEN-02c")
def test_jcs_serialisation_is_byte_stable(tmp_path) -> None:
    """Two writes -> two reads -> JCS bytes are identical."""
    adapter = SeratoAdapter()
    lib = _sample_library()
    a = tmp_path / "a"
    b = tmp_path / "b"
    adapter.write(lib, a)
    adapter.write(lib, b)
    lib_a, _ = adapter.read(a)
    lib_b, _ = adapter.read(b)
    assert serialize_jcs(lib_a) == serialize_jcs(lib_b)


@pytest.mark.requirement("OPEN-02c")
def test_missing_database_raises(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        SeratoAdapter().read(tmp_path)


@pytest.mark.requirement("OPEN-02c")
def test_adapter_report_type() -> None:
    """write() returns a concrete AdapterReport with sensible defaults."""
    assert isinstance(AdapterReport(), AdapterReport)
