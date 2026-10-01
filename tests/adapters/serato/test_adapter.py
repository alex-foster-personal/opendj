"""End-to-end SeratoAdapter tests (OPEN-02c).

Exercises the Protocol-level read/write path by writing a synthetic library,
reading it back, and asserting the open-dj JCS bytes are stable.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from apps.adapters.serato import SeratoAdapter, SeratoAdapterOptions
from apps.open_dj import (
    Adapter,
    AdapterReport,
    BeatGridPoint,
    CuePoint,
    OpenDjLibrary,
    Playlist,
    Track,
    serialize_jcs,
)

_STUB_MP3: Path = (
    Path(__file__).resolve().parents[2] / "fixtures" / "phase7-dedup" / "src-128.mp3"
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

pytestmark = pytest.mark.requirement("OPEN-02")


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


@pytest.mark.requirement("OPEN-02c")
def test_subcrate_playlist_name_cannot_escape_subcrates_dir(tmp_path) -> None:
    """Regression: a hostile playlist name like ``../../evil`` must not
    cause the subcrate file to be written outside the Subcrates/ dir.
    """
    adapter = SeratoAdapter()
    track = Track(
        track_id="ignored",
        file_path="Music/fixture/x.mp3",
        title="X",
        artists=(),
    )
    lib = OpenDjLibrary(
        version="0.1",
        tracks=(track,),
        playlists=(
            Playlist(name="../../evil", track_ids=(track.track_id,)),
        ),
    )
    target = tmp_path / "_Serato_"
    adapter.write(lib, target)

    subcrates_dir = (target / "Subcrates").resolve()
    # Every *.crate file must live directly inside Subcrates/, never above.
    crate_files = list(subcrates_dir.glob("*.crate"))
    assert len(crate_files) == 1, f"expected one crate, got {crate_files!r}"
    crate_path = crate_files[0].resolve()
    assert subcrates_dir in crate_path.parents or crate_path.parent == subcrates_dir, (
        f"subcrate file {crate_path} escaped Subcrates dir {subcrates_dir}"
    )
    # And nothing should have been created up the tree.
    assert not (target.parent / "evil.crate").exists()
    assert not (target.parent.parent / "evil.crate").exists()


@pytest.mark.requirement("OPEN-02c")
def test_playlist_membership_roundtrip(tmp_path) -> None:
    """Regression: subcrate membership must survive write -> read.

    Before the fix, crate-read built track IDs with title="" while
    library-read built them with the real title, so every playlist came
    back with a bogus track_id that matched no library track and
    membership was silently lost.
    """
    adapter = SeratoAdapter()
    track = Track(
        track_id="ignored_on_write",
        file_path="Music/fixture/only.mp3",
        title="Only Song",
        artists=("Solo",),
        album="Fixture",
        bpm=120.0,
        key_camelot="1A",
    )
    lib = OpenDjLibrary(
        version="0.1",
        tracks=(track,),
        playlists=(Playlist(name="main", track_ids=(track.track_id,)),),
    )
    target = tmp_path / "_Serato_"
    adapter.write(lib, target)

    lib_back, _ = adapter.read(target)
    assert len(lib_back.playlists) == 1
    pl_back = lib_back.playlists[0]
    assert pl_back.name == "main"
    assert len(pl_back.track_ids) == 1
    assert pl_back.track_ids[0] == lib_back.tracks[0].track_id, (
        "subcrate membership lost: playlist track_id does not match any "
        "library track_id (crate read must key _stable_track_id on the "
        "track's title, not the empty string)"
    )


# ====================================================================
# GEOB cue + beatgrid write/read round-trip via apps.shared.id3v2 (GH #2 / P0).
# ====================================================================


def _stage_mp3(tmp_path: Path, rel: str = "audio/track.mp3") -> tuple[Path, Path]:
    """Copy the stub MP3 under ``tmp_path/<rel>``. Returns (audio_root, full_path).

    ``audio_root`` is the directory passed to ``SeratoAdapterOptions.audio_root``;
    the adapter resolves ``Track.file_path`` against it so tests don't have to
    bake tmp paths into the library under test.
    """
    assert _STUB_MP3.exists(), f"missing stub MP3 fixture at {_STUB_MP3}"
    audio_root = tmp_path / "audio_root"
    full = audio_root / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_STUB_MP3, full)
    return audio_root, full


@pytest.mark.requirement("OPEN-02c")
def test_geob_cues_roundtrip_via_real_mp3(tmp_path) -> None:
    """Library with hot cues + loop -> Serato write -> read back -> identical cues."""
    audio_root, _ = _stage_mp3(tmp_path)
    adapter = SeratoAdapter(
        options=SeratoAdapterOptions(
            audio_root=audio_root, backup_dir=tmp_path / "_backups",
        )
    )
    cues = (
        CuePoint(index=0, position_ms=0, type="hot", name="Intro", color_rgb=0xCC0000),
        CuePoint(index=1, position_ms=15_000, type="hot", name="Verse", color_rgb=0x00CC00),
        CuePoint(index=2, position_ms=30_000, type="hot", name="Chorus", color_rgb=0x0000CC),
        CuePoint(
            index=3,
            position_ms=45_000,
            length_ms=4_000,
            type="loop",
            name="Break",
            color_rgb=0xCCCC00,
        ),
    )
    track = Track(
        track_id="tr",
        file_path="audio/track.mp3",
        title="GEOB Round Trip",
        artists=("Alice",),
        bpm=128.0,
        cues=cues,
    )
    lib = OpenDjLibrary(version="0.1", tracks=(track,))
    target = tmp_path / "_Serato_"
    write_report = adapter.write(lib, target)
    assert write_report.counts.get("geob_frames_written") == 1

    lib_back, read_report = adapter.read(target)
    assert read_report.counts.get("geob_frames_read", 0) == 1
    back_track = lib_back.tracks[0]
    # Order in Markers2: hots first, then loops (stable per _markers2_to_opendj_cues).
    back_hots = tuple(c for c in back_track.cues if c.type == "hot")
    back_loops = tuple(c for c in back_track.cues if c.type == "loop")
    assert len(back_hots) == 3
    assert len(back_loops) == 1
    # Hot cues: position + index + colour preserved.
    assert [(c.index, c.position_ms, c.name, c.color_rgb) for c in back_hots] == [
        (0, 0, "Intro", 0xCC0000),
        (1, 15_000, "Verse", 0x00CC00),
        (2, 30_000, "Chorus", 0x0000CC),
    ]
    # Loop: start + length round-trip.
    lo = back_loops[0]
    assert lo.index == 3
    assert lo.position_ms == 45_000
    assert lo.length_ms == 4_000
    assert lo.color_rgb == 0xCCCC00


@pytest.mark.requirement("OPEN-02c")
def test_geob_beatgrid_roundtrip(tmp_path) -> None:
    """Beatgrid written to GEOB frame round-trips back through the adapter."""
    audio_root, _ = _stage_mp3(tmp_path)
    adapter = SeratoAdapter(
        options=SeratoAdapterOptions(
            audio_root=audio_root, backup_dir=tmp_path / "_backups",
        )
    )
    beats = (
        BeatGridPoint(position_ms=0, bpm=124.0),
        BeatGridPoint(position_ms=120_000, bpm=124.0, terminal=True),
    )
    track = Track(
        track_id="beatgrid",
        file_path="audio/track.mp3",
        title="Variable Grid",
        artists=("BeatsAuthor",),
        bpm=124.0,
        beats=beats,
    )
    lib = OpenDjLibrary(version="0.1", tracks=(track,))
    adapter.write(lib, tmp_path / "_Serato_")
    lib_back, _ = adapter.read(tmp_path / "_Serato_")
    back = lib_back.tracks[0]
    assert len(back.beats) == 2
    # Position round-trips to the ms (we stash seconds in the Serato frame).
    assert back.beats[0].position_ms == 0
    assert back.beats[-1].position_ms == 120_000
    assert back.beats[-1].terminal is True
    # Terminal BPM propagates back.
    assert back.beats[-1].bpm == pytest.approx(124.0)


@pytest.mark.requirement("OPEN-02c")
def test_geob_write_skips_missing_audio_with_warning(tmp_path) -> None:
    """Missing audio file -> structured warning, crate DB still written."""
    adapter = SeratoAdapter(
        options=SeratoAdapterOptions(audio_root=tmp_path / "does_not_exist")
    )
    track = Track(
        track_id="missing",
        file_path="nope.mp3",
        title="Orphan",
        artists=("X",),
        cues=(CuePoint(index=0, position_ms=0, type="hot"),),
    )
    lib = OpenDjLibrary(version="0.1", tracks=(track,))
    report = adapter.write(lib, tmp_path / "_Serato_")
    # Database V2 still emitted.
    assert (tmp_path / "_Serato_" / "database V2").exists()
    # No GEOB frame written because the file didn't exist.
    assert report.counts.get("geob_frames_written", 0) == 0
    # Structured warning flagged.
    assert any(
        w.action == "dropped" and "missing" in w.reason for w in report.warnings
    )


@pytest.mark.requirement("OPEN-02c")
def test_geob_write_skips_non_mp3_with_warning(tmp_path) -> None:
    """Non-MP3 audio file -> structured warning, crate DB still written."""
    audio_root = tmp_path / "audio_root"
    (audio_root / "x.flac").parent.mkdir(parents=True, exist_ok=True)
    (audio_root / "x.flac").write_bytes(b"fLaC" + b"\x00" * 32)
    adapter = SeratoAdapter(options=SeratoAdapterOptions(audio_root=audio_root))
    track = Track(
        track_id="flac",
        file_path="x.flac",
        title="FLAC out of scope",
        artists=("X",),
        cues=(CuePoint(index=0, position_ms=500, type="hot"),),
    )
    lib = OpenDjLibrary(version="0.1", tracks=(track,))
    report = adapter.write(lib, tmp_path / "_Serato_")
    assert report.counts.get("geob_frames_written", 0) == 0
    assert any(".flac" in w.reason or "mp3" in w.reason for w in report.warnings)


@pytest.mark.requirement("OPEN-02c")
def test_geob_write_backs_up_mp3_before_mutation(tmp_path: Path) -> None:
    """Rail 2 regression (adversarial #2, HIGH): a GEOB write MUST
    produce a pre-mutation backup copy of the MP3 on disk before any
    GEOB mutation runs. Pre-fix ``_write_geob_for_library`` claimed
    Rail 2 in its docstring but took no backup.
    """
    import hashlib

    audio_root, full = _stage_mp3(tmp_path)
    backup_dir = tmp_path / "serato_backups"
    # Capture the pre-write sha256 of the MP3 -- the backup record should
    # carry the same digest.
    pre_sha = hashlib.sha256(full.read_bytes()).hexdigest()

    adapter = SeratoAdapter(
        options=SeratoAdapterOptions(
            audio_root=audio_root, backup_dir=backup_dir,
        )
    )
    track = Track(
        track_id="backup-regression",
        file_path="audio/track.mp3",
        title="Rail 2 guard",
        artists=("Test",),
        cues=(
            CuePoint(index=0, position_ms=0, type="hot", name="Intro"),
        ),
    )
    lib = OpenDjLibrary(version="0.1", tracks=(track,))
    report = adapter.write(lib, tmp_path / "_Serato_")
    assert report.counts.get("geob_frames_written") == 1

    # A backup file must exist under the configured backup_dir, and its
    # contents must hash to the pre-write MP3 sha256.
    backups = list(backup_dir.glob("*.bak"))
    assert len(backups) == 1, f"expected exactly one .bak, got {backups}"
    backup_sha = hashlib.sha256(backups[0].read_bytes()).hexdigest()
    assert backup_sha == pre_sha, (
        "backup was taken AFTER the mutation instead of before; "
        f"backup_sha={backup_sha} pre_sha={pre_sha}"
    )


@pytest.mark.requirement("OPEN-02c")
def test_geob_write_skips_track_when_backup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rail 2 hardening: if the backup step raises we MUST skip the track
    with a structured warning, NOT proceed to mutate the MP3 unbacked.
    """
    audio_root, _ = _stage_mp3(tmp_path)
    backup_dir = tmp_path / "serato_backups"
    adapter = SeratoAdapter(
        options=SeratoAdapterOptions(
            audio_root=audio_root, backup_dir=backup_dir,
        )
    )

    def _boom(*_a: object, **_kw: object) -> None:
        raise OSError("simulated disk full")

    monkeypatch.setattr(
        "apps.adapters.serato.adapter.backup_file", _boom,
    )

    track = Track(
        track_id="backup-fails",
        file_path="audio/track.mp3",
        title="Should not be mutated",
        artists=("Test",),
        cues=(CuePoint(index=0, position_ms=0, type="hot"),),
    )
    lib = OpenDjLibrary(version="0.1", tracks=(track,))
    report = adapter.write(lib, tmp_path / "_Serato_")
    assert report.counts.get("geob_frames_written", 0) == 0
    assert any(
        "backup failed" in w.reason and "Rail 2" in w.reason
        for w in report.warnings
    )



# ---------------------------------------------------------- Codex P16-F01


@pytest.mark.requirement("OPEN-02")
def test_serato_playlist_membership_roundtrip(tmp_path) -> None:
    """Regression for Codex finding P16-F01.

    The Serato adapter hashes track IDs over ``file_path|title`` but
    previously hashed playlist-entry paths over ``file_path|""``. The
    two key spaces never intersected, so every subcrate came back with
    track_ids that matched no track row; playlist membership was
    silently broken on READ. After the fix, crate entries resolve via a
    file_path -> track_id map built from the just-read tracks.
    """
    adapter = SeratoAdapter()
    t1 = Track(
        track_id="ignored",
        file_path="Music/fixture/01.mp3",
        title="Sample One",
        artists=("Alice",),
        bpm=128.0,
    )
    t2 = Track(
        track_id="ignored",
        file_path="Music/fixture/02.mp3",
        title="Sample Two",
        artists=("Bob",),
        bpm=124.5,
    )
    lib = OpenDjLibrary(
        version="0.1",
        tracks=(t1, t2),
        playlists=(Playlist(name="warmup", track_ids=(t1.track_id, t2.track_id)),),
    )
    target = tmp_path / "_Serato_"
    adapter.write(lib, target)

    lib_back, _ = adapter.read(target)
    assert len(lib_back.playlists) == 1
    track_ids = {t.track_id for t in lib_back.tracks}
    pl_members = set(lib_back.playlists[0].track_ids)
    assert pl_members, "playlist must not be empty after round-trip"
    # Every playlist member must resolve to a real track row.
    assert pl_members.issubset(track_ids), (
        f"playlist members {pl_members} not in track set {track_ids}"
    )
    assert len(lib_back.playlists[0].track_ids) == 2
