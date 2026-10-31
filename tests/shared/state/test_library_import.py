"""Serato and Traktor libraries reach the Open DJ library (DJIMPORT-01..04).

[if] a DJ app's library is imported [then] its tracks, fields, cues and playlists land in state.db, [else stop].

Every library here is invented: made-up titles, made-up paths under
``tmp_path``. No row from a real DJ library is used.
"""
from __future__ import annotations

import errno
import json
import struct
import unicodedata
import wave
from pathlib import Path

import pytest

from apps.adapters import library_import as vendor
from apps.adapters.serato.adapter import SeratoAdapter
from apps.open_dj import CuePoint, OpenDjLibrary, Playlist, Track
from apps.shared.state import cue_store
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter

pytestmark = pytest.mark.requirement("DJIMPORT-01")

_STAMP = "2026-10-08T04:00:00+00:00"


def _wav(path: Path, frames: int = 1) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(struct.pack("<h", 0) * frames)
    return path


def _traktor_dir(path: Path) -> str:
    """``/a/b/`` in Traktor's DIR encoding, ``/:a/:b/:``."""
    return "/:" + "/:".join(path.parts[1:]) + "/:"


def _nml(entries: list[str], playlists: str = "") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="no" ?>\n'
        '<NML VERSION="19"><HEAD COMPANY="www.native-instruments.com" PROGRAM="Traktor"/>'
        f'<COLLECTION ENTRIES="{len(entries)}">' + "".join(entries) + "</COLLECTION>"
        + playlists + "</NML>"
    )


def _entry(audio: Path, *, title: str, bpm: str = "", key: str = "", ranking: str = "",
           comment: str = "", playcount: str = "", cues: str = "") -> str:
    info = f'<INFO RANKING="{ranking}" COMMENT="{comment}" PLAYCOUNT="{playcount}" PLAYTIME="200"/>'
    tempo = f'<TEMPO BPM="{bpm}" BPM_QUALITY="100.000000"/>' if bpm else ""
    musical = f'<MUSICAL_KEY VALUE="{key}"/>' if key else ""
    return (
        f'<ENTRY TITLE="{title}" ARTIST="Invented Artist">'
        f'<LOCATION DIR="{_traktor_dir(audio.parent)}" FILE="{audio.name}" '
        f'VOLUME="Macintosh HD" VOLUMEID="abc"/>'
        f"{info}{tempo}{musical}{cues}</ENTRY>"
    )


def _playlist(name: str, audios: list[Path]) -> str:
    keys = "".join(
        f'<ENTRY><PRIMARYKEY TYPE="TRACK" KEY="Macintosh HD{_traktor_dir(a.parent)}{a.name}"/></ENTRY>'
        for a in audios
    )
    return (
        '<PLAYLISTS><NODE TYPE="FOLDER" NAME="$ROOT"><SUBNODES COUNT="1">'
        f'<NODE TYPE="PLAYLIST" NAME="{name}"><PLAYLIST ENTRIES="{len(audios)}" TYPE="LIST">'
        f"{keys}</PLAYLIST></NODE></SUBNODES></NODE></PLAYLISTS>"
    )


def _cues_xml() -> str:
    return (
        '<CUE_V2 NAME="Drop" DISPL_ORDER="0" TYPE="0" START="32000.0" LEN="0" REPEATS="-1" HOTCUE="2"/>'
        '<CUE_V2 NAME="Mix in" DISPL_ORDER="0" TYPE="0" START="1000.0" LEN="0" REPEATS="-1" HOTCUE="-1"/>'
        '<CUE_V2 NAME="Roll" DISPL_ORDER="0" TYPE="5" START="64000.0" LEN="4000.0" REPEATS="-1" HOTCUE="-1"/>'
        '<CUE_V2 NAME="AutoGrid" DISPL_ORDER="0" TYPE="4" START="12.5" LEN="0" REPEATS="-1" HOTCUE="-1"/>'
    )


def _ingest_nml(state_conn, nml_path: Path, *, dry_run: bool = False) -> vendor.VendorIngestReport:
    library, resolver = vendor.read_vendor_library("traktor", nml_path)
    writer = StateWriter(state_conn, bus=FakeEventBus(), actor="vendor-test")
    try:
        return vendor.ingest_library(
            writer, library, vendor="traktor", source=str(nml_path),
            resolve_path=resolver, dry_run=dry_run,
            modified_at=vendor.source_modified_at(nml_path),
        )
    finally:
        writer.close()


def _fields(conn, sid: str) -> dict[str, tuple[object, str]]:
    rows = conn.execute(
        "SELECT field_name, value_json, source FROM track_fields WHERE stable_id = ?", (sid,)
    ).fetchall()
    return {name: (json.loads(value), source) for name, value, source in rows}


def _sid_for(conn, path: Path) -> str:
    row = conn.execute("SELECT stable_id FROM tracks WHERE file_path = ?", (str(path),)).fetchone()
    assert row is not None, f"no track for {path}"
    return str(row[0])


@pytest.mark.requirement("DJIMPORT-01")
def test_traktor_collection_becomes_library_tracks_with_fields_cues_and_playlists(
    state_conn, tmp_path: Path
) -> None:
    """[if] a Traktor collection is imported [then] tracks, BPM, key, rating, comments, pads, memory cues, loops and playlists land, [else stop]."""
    a = _wav(tmp_path / "music" / "Invented Tune One.wav")
    b = _wav(tmp_path / "music" / "Invented Tune Two.wav", frames=2)
    nml = tmp_path / "collection.nml"
    nml.write_text(_nml(
        [
            _entry(a, title="Invented Tune One", bpm="124.000000", key="21", ranking="204",
                   comment="peak time", playcount="7", cues=_cues_xml()),
            _entry(b, title="Invented Tune Two", bpm="98.500000"),
        ],
        _playlist("Invented Warmup", [b, a]),
    ))

    report = _ingest_nml(state_conn, nml)

    assert report.entries_seen == 2
    assert report.tracks_inserted == 2
    assert report.tracks_matched == 0
    sid_a, sid_b = _sid_for(state_conn, a), _sid_for(state_conn, b)
    fields = _fields(state_conn, sid_a)
    assert fields["bpm"] == (124.0, "traktor")
    assert fields["key"][1] == "traktor" and fields["key"][0]
    assert fields["rating"] == (4, "traktor")
    assert fields["comments"] == ("peak time", "traktor")

    stored = cue_store.read_row(state_conn, sid_a)
    assert stored is not None
    blob, source = stored
    assert source == "traktor"
    by_kind = {(c["kind"], c["slot"]): c for c in blob["cues"]}
    # HOTCUE="2" is pad C; HOTCUE="-1" on a plain cue is a memory cue.
    assert by_kind[("hot_cue", "C")]["in_ms"] == 32000
    assert by_kind[("memory", None)]["in_ms"] == 1000
    assert by_kind[("loop", None)]["out_ms"] == 68000
    assert report.cues_skipped == 1  # the grid marker has no cue-store kind
    assert len(blob["cues"]) == 3

    (pl_id,) = [r[0] for r in state_conn.execute(
        "SELECT playlist_id FROM playlists WHERE vendor = 'traktor' AND name = 'Invented Warmup'"
    )]
    members = [r[0] for r in state_conn.execute(
        "SELECT stable_id FROM playlist_memberships WHERE playlist_id = ? ORDER BY order_key",
        (pl_id,),
    )]
    assert members == [sid_b, sid_a]
    assert report.playlist_entries_unresolved == 0


@pytest.mark.requirement("DJIMPORT-02")
def test_a_track_already_in_the_library_is_merged_and_keeps_other_sources_values(
    state_conn, tmp_path: Path
) -> None:
    """[if] the file is already a rekordbox track [then] the import merges onto it and keeps rekordbox's BPM, [else stop]."""
    a = _wav(tmp_path / "music" / "Invented Tune One.wav")
    writer = StateWriter(state_conn, bus=FakeEventBus(), actor="seed")
    try:
        writer.upsert_track(
            stable_id="seeded-from-rekordbox", stable_id_tier="inferred",
            title="Invented Tune One", artists=["Invented Artist"], album=None,
            isrc=None, duration_ms=200000, file_path=str(a),
        )
        writer.set_field("seeded-from-rekordbox", "bpm", 125.0, source="rekordbox", modified_at=_STAMP)
    finally:
        writer.close()
    nml = tmp_path / "collection.nml"
    nml.write_text(_nml([_entry(a, title="Invented Tune One", bpm="124.000000", key="21")]))

    report = _ingest_nml(state_conn, nml)

    assert report.tracks_matched == 1
    assert report.tracks_inserted == 0
    assert state_conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 1
    fields = _fields(state_conn, "seeded-from-rekordbox")
    assert fields["bpm"] == (125.0, "rekordbox")  # kept, not replaced
    assert fields["key"][1] == "traktor"  # filled: nobody had a key
    assert report.fields_kept == {"bpm": 1}
    vendor_row = state_conn.execute(
        "SELECT vendor_id FROM track_vendor_ids WHERE stable_id = ? AND vendor = 'traktor'",
        ("seeded-from-rekordbox",),
    ).fetchone()
    assert vendor_row is not None


@pytest.mark.requirement("DJIMPORT-02")
def test_a_reimport_refreshes_its_own_values_and_a_repeat_is_a_no_op(
    state_conn, tmp_path: Path
) -> None:
    """[if] the app's value changes [then] a re-import refreshes it and a repeat writes nothing, [else stop]."""
    a = _wav(tmp_path / "music" / "Invented Tune One.wav")
    nml = tmp_path / "collection.nml"
    nml.write_text(_nml([_entry(a, title="Invented Tune One", bpm="124.000000")]))
    _ingest_nml(state_conn, nml)
    nml.write_text(_nml([_entry(a, title="Invented Tune One", bpm="126.000000")]))

    second = _ingest_nml(state_conn, nml)
    third = _ingest_nml(state_conn, nml)

    assert _fields(state_conn, _sid_for(state_conn, a))["bpm"] == (126.0, "traktor")
    assert second.fields_written == {"bpm": 1}
    assert third.fields_written == {}
    assert third.tracks_matched == 1 and third.tracks_inserted == 0


@pytest.mark.requirement("DJIMPORT-02")
def test_a_path_spelled_nfd_by_the_app_matches_the_nfc_library_track(
    state_conn, tmp_path: Path
) -> None:
    """[if] the app spells a path in NFD [then] it matches the NFC library track, [else stop]."""
    library_path = tmp_path / unicodedata.normalize("NFC", "Café Invented.wav")
    writer = StateWriter(state_conn, bus=FakeEventBus(), actor="seed")
    try:
        writer.upsert_track(
            stable_id="nfc-row", stable_id_tier="inferred", title="Café Invented",
            artists=[], album=None, isrc=None, duration_ms=None, file_path=str(library_path),
        )
    finally:
        writer.close()
    nfd = unicodedata.normalize("NFD", str(library_path))
    lib = OpenDjLibrary(version="0.1", tracks=(Track(track_id="t1", file_path=nfd, bpm=120.0),))
    writer = StateWriter(state_conn, bus=FakeEventBus(), actor="vendor-test")
    try:
        report = vendor.ingest_library(
            writer, lib, vendor="serato", source="fixture", resolve_path=lambda p: p, dry_run=False,
        )
    finally:
        writer.close()
    assert report.tracks_matched == 1
    assert state_conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 1


@pytest.mark.requirement("DJIMPORT-03")
def test_a_removed_track_stays_removed_and_out_of_imported_playlists(
    state_conn, tmp_path: Path
) -> None:
    """[if] the user removed a track [then] an import keeps it removed and out of playlists, [else stop]."""
    a = _wav(tmp_path / "music" / "Invented Tune One.wav")
    b = _wav(tmp_path / "music" / "Invented Tune Two.wav", frames=2)
    nml = tmp_path / "collection.nml"
    nml.write_text(_nml(
        [_entry(a, title="Invented Tune One"), _entry(b, title="Invented Tune Two")],
        _playlist("Invented Set", [a, b]),
    ))
    _ingest_nml(state_conn, nml)
    sid_a, sid_b = _sid_for(state_conn, a), _sid_for(state_conn, b)
    writer = StateWriter(state_conn, bus=FakeEventBus(), actor="user")
    try:
        writer.remove_from_library(sid_a)
    finally:
        writer.close()

    report = _ingest_nml(state_conn, nml)

    assert report.tracks_skipped_deleted == 1
    deleted_at = state_conn.execute(
        "SELECT deleted_at FROM tracks WHERE stable_id = ?", (sid_a,)
    ).fetchone()[0]
    assert deleted_at is not None
    members = [r[0] for r in state_conn.execute(
        "SELECT m.stable_id FROM playlist_memberships m JOIN playlists p USING (playlist_id) "
        "WHERE p.name = 'Invented Set' ORDER BY m.order_key"
    )]
    assert members == [sid_b]


@pytest.mark.requirement("DJIMPORT-01")
def test_dry_run_writes_nothing(state_conn, tmp_path: Path) -> None:
    """[if] the import is a dry-run [then] state.db stays empty, [else stop]."""
    a = _wav(tmp_path / "music" / "Invented Tune One.wav")
    nml = tmp_path / "collection.nml"
    nml.write_text(_nml([_entry(a, title="Invented Tune One", bpm="124.0")], _playlist("P", [a])))

    report = _ingest_nml(state_conn, nml, dry_run=True)

    assert report.tracks_inserted == 1
    for table in ("tracks", "track_fields", "playlists", "track_vendor_ids"):
        assert state_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table


@pytest.mark.requirement("DJIMPORT-01")
def test_the_command_line_dry_runs_by_default_and_writes_with_write(
    state_conn, state_db_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] the import runs from the command line [then] it dry-runs unless --write is given, [else stop]."""
    a = _wav(tmp_path / "music" / "Invented Tune One.wav")
    nml = tmp_path / "collection.nml"
    nml.write_text(_nml([_entry(a, title="Invented Tune One", bpm="124.0")]))
    argv = ["traktor", "--source", str(nml), "--db", str(state_db_path)]

    assert vendor.main(argv) == 0
    assert "traktor import (dry-run)" in capsys.readouterr().out
    assert state_conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 0

    assert vendor.main([*argv, "--write"]) == 0
    assert state_conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 1
    assert vendor.main(["traktor", "--source", str(tmp_path / "missing.nml"), "--db", str(state_db_path)]) == 1


@pytest.mark.requirement("DJIMPORT-01")
def test_serato_library_imports_bpm_key_and_crates(state_conn, tmp_path: Path) -> None:
    """[if] a Serato library is imported [then] BPM, key and crate order land, [else stop]."""
    a = _wav(tmp_path / "music" / "Invented Serato One.wav")
    b = _wav(tmp_path / "music" / "Invented Serato Two.wav", frames=2)
    serato_dir = tmp_path / "_Serato_"
    # Serato stores pfil relative to the root of the drive it lives on, which
    # is whatever mount holds tmp_path on this host (a CI runner's home can be
    # its own mount, so "/" is not assumed).
    drive = vendor.serato_volume_root(serato_dir)
    rel = lambda p: str(p.relative_to(drive))  # noqa: E731
    SeratoAdapter().write(
        OpenDjLibrary(
            version="0.1",
            tracks=(
                Track(track_id="s1", file_path=rel(a), title="Invented Serato One", bpm=122.0, key_camelot="5A"),
                Track(track_id="s2", file_path=rel(b), title="Invented Serato Two", bpm=90.0),
            ),
            playlists=(Playlist(name="Invented Crate", track_ids=("s2", "s1")),),
        ),
        serato_dir,
    )
    library, resolver = vendor.read_vendor_library("serato", serato_dir)
    writer = StateWriter(state_conn, bus=FakeEventBus(), actor="vendor-test")
    try:
        report = vendor.ingest_library(
            writer, library, vendor="serato", source=str(serato_dir),
            resolve_path=resolver, dry_run=False,
        )
    finally:
        writer.close()

    assert report.tracks_inserted == 2 and report.files_missing == 0
    sid_a, sid_b = _sid_for(state_conn, a), _sid_for(state_conn, b)
    assert _fields(state_conn, sid_a)["bpm"] == (122.0, "serato")
    assert _fields(state_conn, sid_a)["key"] == ("5A", "serato")
    members = [r[0] for r in state_conn.execute(
        "SELECT m.stable_id FROM playlist_memberships m JOIN playlists p USING (playlist_id) "
        "WHERE p.vendor = 'serato' AND p.name = 'Invented Crate' ORDER BY m.order_key"
    )]
    assert members == [sid_b, sid_a]


@pytest.mark.requirement("DJIMPORT-04")
def test_traktor_volume_paths_resolve_to_the_system_drive_or_a_mounted_volume(tmp_path: Path) -> None:
    """[if] Traktor names a volume [then] the path resolves to the system drive, a mounted volume or a drive letter, [else stop]."""
    resolve = vendor.traktor_path_resolver(volumes_root=tmp_path / "Volumes")
    on_disk = _wav(tmp_path / "x.wav")
    assert resolve(f"Macintosh HD{on_disk}") == str(on_disk)
    assert resolve("C:/Music/x.mp3") == "C:/Music/x.mp3"
    # Nothing on disk: the system-drive reading is kept, so a relink can find it.
    assert resolve("Invented Stick/Music/none.mp3") == "/Music/none.mp3"


@pytest.mark.requirement("DJIMPORT-04")
def test_traktor_prefers_the_named_volume_over_the_same_path_on_the_system_drive(
    tmp_path: Path,
) -> None:
    """[if] the same relative path exists on the system drive and on the named volume [then] the named volume's file wins, [else stop]."""
    volumes = tmp_path / "Volumes"
    resolve = vendor.traktor_path_resolver(volumes_root=volumes)
    on_system = _wav(tmp_path / "same.wav")
    on_stick = _wav(volumes / "Invented Stick" / str(on_system).lstrip("/"))
    assert resolve(f"Invented Stick{on_system}") == str(on_stick)
    # Control: with the stick unplugged the system drive still answers.
    on_stick.unlink()
    assert resolve(f"Invented Stick{on_system}") == str(on_system)


@pytest.mark.requirement("DJIMPORT-04")
def test_traktor_system_volume_link_reads_as_the_system_path(tmp_path: Path) -> None:
    """[if] the named volume links to the system drive (macOS /Volumes/Macintosh HD) [then] the path reads from /, not via the link, [else stop]."""
    volumes = tmp_path / "Volumes"
    volumes.mkdir()
    (volumes / "Macintosh HD").symlink_to("/")
    resolve = vendor.traktor_path_resolver(volumes_root=volumes)
    on_disk = _wav(tmp_path / "system.wav")
    assert resolve(f"Macintosh HD{on_disk}") == str(on_disk)
    # Control: a real external volume is still read through /Volumes.
    on_stick = _wav(volumes / "Invented Stick" / "Music" / "a.wav")
    assert resolve("Invented Stick/Music/a.wav") == str(on_stick)


@pytest.mark.requirement("DJIMPORT-04")
def test_serato_relative_paths_hang_from_the_drive_root(tmp_path: Path) -> None:
    """[if] Serato stores a drive-relative path [then] it resolves from the drive root, [else stop]."""
    resolve = vendor.serato_path_resolver(
        tmp_path / "home" / "Music" / "_Serato_", is_mount=lambda path: path == Path("/")
    )
    assert resolve("Users/dj/Music/a.mp3") == "/Users/dj/Music/a.mp3"
    assert resolve("/already/absolute.mp3") == "/already/absolute.mp3"


@pytest.mark.requirement("DJIMPORT-04")
@pytest.mark.parametrize("serato_rel", ["_Serato_", "Music/_Serato_", "a/b/_Serato_"])
def test_serato_paths_hang_from_the_mount_holding_the_library_at_any_depth(
    tmp_path: Path, serato_rel: str
) -> None:
    """[if] _Serato_ sits anywhere on a mounted drive [then] its relative paths resolve from that drive's mount point, [else stop]."""
    stick = tmp_path / "Volumes" / "Invented Stick"

    def is_mount(path: Path) -> bool:
        return path in (stick, Path("/"))

    resolve = vendor.serato_path_resolver(stick / serato_rel, is_mount=is_mount)
    assert resolve("Music/a.mp3") == str(stick / "Music" / "a.mp3")


@pytest.mark.requirement("DJIMPORT-01")
def test_an_unreadable_audio_file_stops_the_import_naming_it(
    state_conn, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] an existing audio file cannot be read to hash it [then] the import stops naming the file, never writing a hashless track, [else stop]."""
    audio = _wav(tmp_path / "unreadable.wav")
    library = OpenDjLibrary(
        version="0.1", tracks=(Track(track_id="1", file_path=str(audio), title="Invented"),)
    )
    real_hash = vendor.hashing.sha256_audio_payload

    def failing_read(path: object) -> str:
        if str(path) == str(audio):
            raise OSError(errno.EIO, "Input/output error")
        return real_hash(path)

    monkeypatch.setattr(vendor.hashing, "sha256_audio_payload", failing_read)
    writer = StateWriter(state_conn, bus=FakeEventBus(), actor="vendor-test")
    try:
        with pytest.raises(OSError, match=r"unreadable\.wav: cannot read audio"):
            vendor.ingest_library(
                writer, library, vendor="traktor", source=str(tmp_path),
                resolve_path=str, dry_run=False, modified_at=_STAMP,
            )
    finally:
        writer.close()
    assert state_conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 0


@pytest.mark.requirement("DJIMPORT-01")
def test_a_missing_audio_file_still_imports_as_missing(state_conn, tmp_path: Path) -> None:
    """[if] a library row points at a file that is not on disk [then] it imports and counts as missing, not as a read failure, [else stop]."""
    gone = tmp_path / "not-here.wav"
    library = OpenDjLibrary(
        version="0.1", tracks=(Track(track_id="1", file_path=str(gone), title="Invented"),)
    )
    writer = StateWriter(state_conn, bus=FakeEventBus(), actor="vendor-test")
    try:
        report = vendor.ingest_library(
            writer, library, vendor="traktor", source=str(tmp_path),
            resolve_path=str, dry_run=False, modified_at=_STAMP,
        )
    finally:
        writer.close()
    assert report.files_missing == 1 and report.tracks_inserted == 1


@pytest.mark.requirement("DJIMPORT-01")
def test_cue_mapping_keeps_pads_and_never_invents_kinds() -> None:
    """[if] an app cue has a pad or no store kind [then] pads are kept and unknown kinds are counted, not invented, [else stop]."""
    report = vendor.VendorIngestReport(vendor="serato", source="unit")
    cues = vendor.store_cues(
        [
            CuePoint(index=0, position_ms=100, type="hot"),
            CuePoint(index=9, position_ms=200, type="hot"),  # no pad I+
            CuePoint(index=0, position_ms=300, type="loop", length_ms=500),
            CuePoint(index=1, position_ms=400, type="loop"),  # no length: not a loop
            CuePoint(index=0, position_ms=500, type="load"),
        ],
        report,
    )
    assert [(c["kind"], c["slot"], c["in_ms"], c["out_ms"]) for c in cues] == [
        ("hot_cue", "A", 100, None),
        ("memory", None, 200, None),
        ("loop", None, 300, 800),
    ]
    assert report.cues_skipped == 2
    for cue in cues:
        cue_store.normalize_cue(cue)  # every mapped cue is valid for the store


def _rb_xml(audio: Path, *, bpm: str = "128.00") -> str:
    from urllib.parse import quote

    loc = "file://localhost" + quote(str(audio))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<DJ_PLAYLISTS Version="1.0.0"><PRODUCT Name="Lexicon"/>
<COLLECTION Entries="1">
<TRACK TrackID="7" Name="Invented XML Tune" Artist="Invented Artist" AverageBpm="{bpm}" Tonality="8A"
 Rating="153" Location="{loc}">
<POSITION_MARK Name="Drop" Type="0" Start="30.0" Num="0"/>
</TRACK></COLLECTION>
<PLAYLISTS><NODE Type="0" Name="ROOT" Count="1"><NODE Type="0" Name="Invented Folder" Count="1">
<NODE Name="Invented Night" Type="1" KeyType="0" Entries="1"><TRACK Key="7"/></NODE></NODE></NODE></PLAYLISTS>
</DJ_PLAYLISTS>"""


def _ingest_xml(state_conn, xml_path: Path) -> vendor.VendorIngestReport:
    library, resolver = vendor.read_vendor_library("rekordbox-xml", xml_path)
    writer = StateWriter(state_conn, bus=FakeEventBus(), actor="vendor-test")
    try:
        return vendor.ingest_library(
            writer, library, vendor="rekordbox-xml", source=str(xml_path),
            resolve_path=resolver, dry_run=False,
            modified_at=vendor.source_modified_at(xml_path),
        )
    finally:
        writer.close()


@pytest.mark.requirement("DJIMPORT-06")
def test_rekordbox_xml_imports_fields_cues_and_folder_playlists(state_conn, tmp_path: Path) -> None:
    """[if] a rekordbox XML export is imported [then] fields land as source rekordbox with cues and its playlist, [else stop]."""
    a = _wav(tmp_path / "music" / "Invented XML Tune.wav")
    xml = tmp_path / "export.xml"
    xml.write_text(_rb_xml(a))

    report = _ingest_xml(state_conn, xml)

    assert report.tracks_inserted == 1
    sid = _sid_for(state_conn, a)
    fields = _fields(state_conn, sid)
    assert fields["bpm"] == (128.0, "rekordbox")
    assert fields["key"] == ("8A", "rekordbox")
    assert fields["rating"] == (3, "rekordbox")
    blob, source = cue_store.read_row(state_conn, sid)  # type: ignore[misc]
    assert source == "rekordbox"
    assert [(c["kind"], c["slot"], c["in_ms"]) for c in blob["cues"]] == [("hot_cue", "A", 30000)]
    row = state_conn.execute(
        "SELECT vendor_pl_id FROM playlists WHERE vendor = 'rekordbox-xml' AND name = 'Invented Night'"
    ).fetchone()
    assert row == ("Invented Folder/Invented Night",)
    vendor_ids = {r[0] for r in state_conn.execute("SELECT vendor FROM track_vendor_ids WHERE stable_id = ?", (sid,))}
    assert vendor_ids == {"rekordbox-xml"}  # never the master.db mapping


@pytest.mark.requirement("DJIMPORT-06")
def test_rekordbox_xml_never_replaces_master_db_values_or_cues(state_conn, tmp_path: Path) -> None:
    """[if] master.db already gave a track BPM and cues under source rekordbox [then] an XML import keeps both, [else stop]."""
    a = _wav(tmp_path / "music" / "Invented XML Tune.wav")
    writer = StateWriter(state_conn, bus=FakeEventBus(), actor="seed")
    try:
        writer.upsert_track(
            stable_id="from-master-db", stable_id_tier="inferred", title="Invented XML Tune",
            artists=[], album=None, isrc=None, duration_ms=None, file_path=str(a),
        )
        writer.set_field("from-master-db", "bpm", 127.0, source="rekordbox", modified_at=_STAMP)
        cue_store.import_vendor_cues(
            writer.set_field, state_conn, "from-master-db",
            [{"kind": "hot_cue", "slot": "B", "in_ms": 5000}], source="rekordbox", modified_at=_STAMP,
        )
    finally:
        writer.close()
    xml = tmp_path / "export.xml"
    xml.write_text(_rb_xml(a, bpm="131.00"))

    report = _ingest_xml(state_conn, xml)

    fields = _fields(state_conn, "from-master-db")
    assert fields["bpm"] == (127.0, "rekordbox")
    assert fields["key"] == ("8A", "rekordbox")  # empty, so filled
    assert report.fields_kept == {"bpm": 1}
    blob, _ = cue_store.read_row(state_conn, "from-master-db")  # type: ignore[misc]
    assert [(c["slot"], c["in_ms"]) for c in blob["cues"]] == [("B", 5000)]
    assert report.cue_sets_written == 0


@pytest.mark.requirement("DJIMPORT-06")
def test_rekordbox_xml_playlists_whose_names_contain_a_slash_do_not_collide(state_conn, tmp_path: Path) -> None:
    """[if] folder A holds playlist B/C and folder A/B holds playlist C [then] both keep their own members, [else stop]."""
    a = _wav(tmp_path / "music" / "Invented One.wav")
    b = _wav(tmp_path / "music" / "Invented Two.wav")
    xml = tmp_path / "slash.xml"
    xml.write_text(
        '<DJ_PLAYLISTS Version="1.0.0"><COLLECTION Entries="2">'
        f'<TRACK TrackID="1" Name="One" Location="file://localhost{a}"/>'
        f'<TRACK TrackID="2" Name="Two" Location="file://localhost{b}"/>'
        "</COLLECTION>"
        '<PLAYLISTS><NODE Type="0" Name="ROOT" Count="2">'
        '<NODE Type="0" Name="A" Count="1"><NODE Type="1" Name="B/C" KeyType="0" Entries="1"><TRACK Key="1"/></NODE></NODE>'
        '<NODE Type="0" Name="A/B" Count="1"><NODE Type="1" Name="C" KeyType="0" Entries="1"><TRACK Key="2"/></NODE></NODE>'
        "</NODE></PLAYLISTS></DJ_PLAYLISTS>"
    )

    _ingest_xml(state_conn, xml)

    rows = state_conn.execute(
        "SELECT p.vendor_pl_id, COUNT(m.stable_id) FROM playlists p "
        "JOIN playlist_memberships m USING (playlist_id) WHERE p.vendor = 'rekordbox-xml' "
        "GROUP BY p.vendor_pl_id ORDER BY p.vendor_pl_id"
    ).fetchall()
    assert rows == [("A/B\\/C", 1), ("A\\/B/C", 1)]


@pytest.mark.requirement("DJIMPORT-06")
def test_a_protected_value_equal_to_the_import_is_still_kept_and_counted(state_conn, tmp_path: Path) -> None:
    """[if] another source's BPM equals the imported one [then] it is kept and counted in fields_kept, [else stop]."""
    a = _wav(tmp_path / "music" / "Invented Tune Equal.wav")
    writer = StateWriter(state_conn, bus=FakeEventBus(), actor="seed")
    try:
        writer.upsert_track(
            stable_id="seeded-equal", stable_id_tier="inferred",
            title="Invented Tune Equal", artists=["Invented Artist"], album=None,
            isrc=None, duration_ms=200000, file_path=str(a),
        )
        writer.set_field("seeded-equal", "bpm", 124.0, source="rekordbox", modified_at=_STAMP)
    finally:
        writer.close()
    nml = tmp_path / "equal.nml"
    nml.write_text(_nml([_entry(a, title="Invented Tune Equal", bpm="124.000000")]))

    report = _ingest_nml(state_conn, nml)

    assert report.fields_kept == {"bpm": 1}
    assert _fields(state_conn, "seeded-equal")["bpm"] == (124.0, "rekordbox")


def test_the_playlist_key_escapes_only_for_rekordbox_xml() -> None:
    """[if] a playlist path has a slash [then] only rekordbox-xml escapes it, so other vendors keep their ids, [else stop]."""
    assert vendor._playlist_key("traktor", ("A", "B/C")) == "A/B/C"
    assert vendor._playlist_key("rekordbox-xml", ("A", "B/C")) == "A/B\\/C"
