"""Serato and Traktor libraries reach the Open DJ library (DJIMPORT-01..04).

[if] a DJ app's library is imported [then] its tracks, fields, cues and playlists land in state.db, [else stop].

Every library here is invented: made-up titles, made-up paths under
``tmp_path``. No row from a real DJ library is used.
"""
from __future__ import annotations

import argparse
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
           comment: str = "", playcount: str = "", playtime: str = "200", cues: str = "") -> str:
    info = f'<INFO RANKING="{ranking}" COMMENT="{comment}" PLAYCOUNT="{playcount}" PLAYTIME="{playtime}"/>'
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


@pytest.mark.requirement("DJIMPORT-6039")
def test_the_summary_counts_what_the_reader_could_not_carry_over(
    state_db_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] the app's reader drops a value [then] the import summary counts it by kind, [else stop]."""
    a = _wav(tmp_path / "music" / "Invented Tune One.wav")
    b = _wav(tmp_path / "music" / "Invented Tune Two.wav", frames=2)
    nml = tmp_path / "collection.nml"
    nml.write_text(_nml([
        _entry(a, title="Invented Tune One", bpm="124.0"),
        _entry(b, title="Invented Tune Two", bpm="not-a-number"),
    ]))
    assert vendor.main(["traktor", "--source", str(nml), "--db", str(state_db_path)]) == 0
    out = capsys.readouterr().out
    assert "not carried over by the reader: 1" in out
    assert "1  bpm dropped: unparseable TEMPO @BPM 'not-a-number'" in out


@pytest.mark.requirement("DJIMPORT-6039")
def test_traktor_key_rating_playtime_and_playcount_losses_are_counted(
    state_db_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] a Traktor key, rating, play time or play count cannot be read [then] each is counted as
    dropped, and readable ones are not, [else stop]."""
    a = _wav(tmp_path / "music" / "Invented Tune One.wav")
    b = _wav(tmp_path / "music" / "Invented Tune Two.wav", frames=2)
    c = _wav(tmp_path / "music" / "Invented Tune Three.wav", frames=3)
    huge = "9" * 309  # an integer too large to divide as a float, or to store
    nml = tmp_path / "collection.nml"
    nml.write_text(_nml([
        # control: every value readable, so nothing is counted for this track
        _entry(a, title="Invented Tune One", key="21", ranking="204", playcount="3", playtime="200"),
        _entry(b, title="Invented Tune Two", key="99", ranking="lots", playcount="many", playtime="inf"),
        # oversized integers are counted, never raised out of the import
        _entry(c, title="Invented Tune Three", ranking=huge, playcount=str(2**64), playtime="1e100"),
    ]))
    assert vendor.main(["traktor", "--source", str(nml), "--db", str(state_db_path)]) == 0
    out = capsys.readouterr().out
    assert "not carried over by the reader: 7" in out
    assert "1  duration dropped: unparseable INFO @PLAYTIME '1e100'" in out
    assert f"1  rating dropped: unparseable INFO @RANKING {huge!r}" in out
    assert f"1  play_count dropped: unparseable INFO @PLAYCOUNT '{2**64}'" in out
    assert "1  key dropped: unreadable MUSICAL_KEY @VALUE '99'" in out
    assert "1  rating dropped: unparseable INFO @RANKING 'lots'" in out
    assert "1  duration dropped: unparseable INFO @PLAYTIME 'inf'" in out
    assert "1  play_count dropped: unparseable INFO @PLAYCOUNT 'many'" in out


@pytest.mark.requirement("DJIMPORT-6039")
def test_traktor_negative_play_times_play_counts_and_cue_starts_are_counted_not_stored(
    state_db_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] a Traktor PLAYTIME, PLAYCOUNT or CUE_V2 START is negative [then] it is counted as dropped
    and never stored as a negative duration, count or position, [else stop]."""
    a = _wav(tmp_path / "music" / "Invented Tune One.wav")
    nml = tmp_path / "collection.nml"
    cues = (
        '<CUE_V2 NAME="Negative" DISPL_ORDER="0" TYPE="0" START="-0.4" LEN="0" REPEATS="-1" HOTCUE="0"/>'
        '<CUE_V2 NAME="Kept" DISPL_ORDER="0" TYPE="0" START="1000.0" LEN="0" REPEATS="-1" HOTCUE="1"/>'
    )
    nml.write_text(_nml([_entry(a, title="Invented Tune One", bpm="-1e-999", playcount="-1", playtime="-0.0004", cues=cues)]))
    assert vendor.main(["traktor", "--source", str(nml), "--db", str(state_db_path), "--write"]) == 0
    out = capsys.readouterr().out
    assert "1  bpm dropped: unparseable TEMPO @BPM '-1e-999'" in out
    assert "1  duration dropped: unparseable INFO @PLAYTIME '-0.0004'" in out
    assert "1  play_count dropped: unparseable INFO @PLAYCOUNT '-1'" in out
    assert "1  cue_points.position dropped: unparseable CUE_V2 @START" in out
    library, _ = vendor.read_vendor_library("traktor", nml)
    (track,) = library.tracks
    assert track.bpm is None
    assert track.duration_ms is None
    assert track.play_count == 0
    assert {c.name for c in track.cues} == {"Kept"}
    assert all(c.position_ms >= 0 for c in track.cues)


@pytest.mark.requirement("DJIMPORT-6039")
def test_traktor_a_bpm_that_underflows_to_zero_is_counted_not_stored(
    state_db_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] a Traktor TEMPO @BPM is nonzero but too small to keep as a float [then] it is counted as dropped, not stored
    as a zero BPM, [else stop]."""
    a = _wav(tmp_path / "music" / "Invented Tune One.wav")
    nml = tmp_path / "collection.nml"
    nml.write_text(_nml([_entry(a, title="Invented Tune One", bpm="1e-999")]))
    assert vendor.main(["traktor", "--source", str(nml), "--db", str(state_db_path)]) == 0
    assert "1  bpm dropped: unparseable TEMPO @BPM '1e-999'" in capsys.readouterr().out
    library, _ = vendor.read_vendor_library("traktor", nml)
    (track,) = library.tracks
    assert track.bpm is None


@pytest.mark.requirement("DJIMPORT-6039")
def test_traktor_a_play_time_near_the_store_limit_keeps_its_exact_milliseconds(
    state_conn, tmp_path: Path
) -> None:
    """[if] a Traktor PLAYTIME is a large but storable number of seconds [then] its milliseconds
    are exact, not rounded across the store limit by a float, [else stop]."""
    a = _wav(tmp_path / "music" / "Invented Tune One.wav")
    nml = tmp_path / "collection.nml"
    nml.write_text(_nml([_entry(a, title="Invented Tune One", playtime="9223372036854775")]))
    _ingest_nml(state_conn, nml)
    (duration_ms,) = state_conn.execute(
        "SELECT duration_ms FROM tracks WHERE stable_id = ?", (_sid_for(state_conn, a),)
    ).fetchone()
    assert duration_ms == 9223372036854775000


@pytest.mark.requirement("DJIMPORT-6039")
def test_traktor_a_play_time_on_a_half_millisecond_rounds_to_even_not_by_float_error(
    state_conn, tmp_path: Path
) -> None:
    """[if] a Traktor PLAYTIME is exactly half a millisecond past a whole one [then] it rounds half to even
    on the exact value, so 68526245.2705 s stores 68526245270 ms; a float stored ...271 from its error, [else stop]."""
    a = _wav(tmp_path / "music" / "Invented Tune One.wav")
    nml = tmp_path / "collection.nml"
    nml.write_text(_nml([_entry(a, title="Invented Tune One", playtime="68526245.2705")]))
    _ingest_nml(state_conn, nml)
    (duration_ms,) = state_conn.execute(
        "SELECT duration_ms FROM tracks WHERE stable_id = ?", (_sid_for(state_conn, a),)
    ).fetchone()
    assert duration_ms == 68526245270


@pytest.mark.requirement("DJIMPORT-6039")
def test_traktor_non_finite_bpm_and_location_less_entries_are_counted(
    state_db_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] a Traktor BPM is NaN or infinite, or an entry has no file location [then] each is counted
    as dropped and no stand-in track is made, [else stop]."""
    a = _wav(tmp_path / "music" / "Invented Tune One.wav")
    b = _wav(tmp_path / "music" / "Invented Tune Two.wav", frames=2)
    c = _wav(tmp_path / "music" / "Invented Tune Three.wav", frames=3)
    no_location = '<ENTRY TITLE="Invented Nowhere" ARTIST="Invented Artist"><INFO PLAYTIME="200"/></ENTRY>'
    nml = tmp_path / "collection.nml"
    nml.write_text(_nml([
        _entry(a, title="Invented Tune One", bpm="124.0"),  # control: a finite BPM adds nothing
        _entry(b, title="Invented Tune Two", bpm="nan"),
        _entry(c, title="Invented Tune Three", bpm="inf"),
        no_location,
        no_location.replace("Invented Nowhere", "Invented Nowhere Two"),
        # a partial LOCATION (volume or directory, no file) is no file location either
        '<ENTRY TITLE="Invented Volume Only" ARTIST="Invented Artist"><LOCATION VOLUME="Macintosh HD"/></ENTRY>',
        '<ENTRY TITLE="Invented Dir Only" ARTIST="Invented Artist"><LOCATION DIR="/:Invented/:" VOLUME="Macintosh HD"/></ENTRY>',
    ]))
    assert vendor.main(["traktor", "--source", str(nml), "--db", str(state_db_path)]) == 0
    out = capsys.readouterr().out
    assert "not carried over by the reader: 6" in out
    assert "1  bpm dropped: unparseable TEMPO @BPM 'nan'" in out
    assert "1  bpm dropped: unparseable TEMPO @BPM 'inf'" in out
    assert "4  location dropped: collection entry has no file location" in out
    # the dropped entries stay in the source total, not just the loss list
    assert "entries in source:    7" in out
    assert "dropped by reader:    4" in out
    assert "entries in library:   3" in out
    # an infinite cue START is counted, not raised out of the import
    d = _wav(tmp_path / "music" / "Invented Tune Four.wav", frames=4)
    cue_nml = tmp_path / "cues.nml"
    broken_cues = (
        _cues_xml()
        .replace('START="32000.0"', 'START="inf"')
        .replace(' START="64000.0"', "")
        .replace('START="1000.0"', 'START="1e300"')
    )
    cue_nml.write_text(_nml([_entry(d, title="Invented Tune Four", cues=broken_cues)]))
    # written for real, so an oversized value would have to fit the store
    assert vendor.main(["traktor", "--source", str(cue_nml), "--db", str(state_db_path), "--write"]) == 0
    cue_out = capsys.readouterr().out
    assert "2  cue_points.position dropped: unparseable CUE_V2 @START" in cue_out  # inf and 1e300
    assert "1  cue_points.position dropped: CUE_V2 has no @START" in cue_out
    # the dropped cues are gone, not placed at 0; their siblings are kept
    cue_library, _ = vendor.read_vendor_library("traktor", cue_nml)
    (cue_track,) = cue_library.tracks
    assert not {"Drop", "Roll", "Mix in"} & {c.name for c in cue_track.cues}
    assert 0 not in {c.position_ms for c in cue_track.cues}
    assert "AutoGrid" in {c.name for c in cue_track.cues}
    library, _ = vendor.read_vendor_library("traktor", nml)
    assert sorted(t.title for t in library.tracks) == ["Invented Tune One", "Invented Tune Three", "Invented Tune Two"]

    # Control: a clean library says so with a zero, not by printing nothing.
    nml.write_text(_nml([_entry(a, title="Invented Tune One", bpm="124.0")]))
    assert vendor.main(["traktor", "--source", str(nml), "--db", str(state_db_path)]) == 0
    clean_out = capsys.readouterr().out
    assert "not carried over by the reader: 0" in clean_out
    assert "entries in source:    1" in clean_out and "dropped by reader:    0" in clean_out


@pytest.mark.requirement("DJIMPORT-6039")
def test_a_rekordbox_xml_track_with_no_location_is_counted_as_not_carried_over(tmp_path: Path) -> None:
    """[if] a rekordbox XML track has no file location [then] the read counts it, [else stop]."""
    xml = tmp_path / "rekordbox.xml"
    xml.write_text(
        '<DJ_PLAYLISTS Version="1.0.0"><COLLECTION Entries="2">'
        '<TRACK TrackID="1" Name="Invented Local" Location="file://localhost/Volumes/Invented/a.mp3"/>'
        '<TRACK TrackID="2" Name="Invented Stream" Location=""/>'
        "</COLLECTION></DJ_PLAYLISTS>"
    )
    read = vendor.read_vendor("rekordbox-xml", xml)
    assert len(read.library.tracks) == 1
    assert read.reader_notes == {"location dropped: streaming or blank": 1}
    assert read.entries_dropped == 1  # kept in the source total


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


@pytest.mark.requirement("DJIMPORT-6039")
def test_a_serato_row_with_no_file_path_is_counted_as_not_carried_over(tmp_path: Path) -> None:
    """[if] a Serato database row has no file path [then] the read counts it and keeps it out, [else stop]."""
    a = _wav(tmp_path / "music" / "Invented Serato Blank Path.wav")
    serato_dir = tmp_path / "_Serato_"
    drive = vendor.serato_volume_root(serato_dir)
    SeratoAdapter().write(
        OpenDjLibrary(
            version="0.1",
            tracks=(
                Track(track_id="s1", file_path=str(a.relative_to(drive)), title="Invented Serato Blank Path"),
                Track(track_id="s2", file_path="", title="Invented Serato Blank Row"),
            ),
        ),
        serato_dir,
    )
    read = vendor.read_vendor("serato", serato_dir)
    assert [t.title for t in read.library.tracks] == ["Invented Serato Blank Path"]
    assert read.reader_notes == {"location dropped: database row has no file path": 1}
    assert read.entries_dropped == 1  # kept in the source total


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


def _rb_xml(audio: Path, *, bpm: str = "128.00", extra: str = "") -> str:
    from urllib.parse import quote

    loc = "file://localhost" + quote(str(audio))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<DJ_PLAYLISTS Version="1.0.0"><PRODUCT Name="Lexicon"/>
<COLLECTION Entries="1">
<TRACK TrackID="7" Name="Invented XML Tune" Artist="Invented Artist" AverageBpm="{bpm}" Tonality="8A"
 Rating="153" {extra} Location="{loc}">
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


def _rb_xml_tracks(audios: list[Path], extra: str) -> str:
    from urllib.parse import quote

    parts = [
        f'<TRACK TrackID="{idx + 1}" Name="Invented XML Tune {idx + 1}" Artist="Invented Artist" '
        f'AverageBpm="128.00" Tonality="8A" Rating="153" {extra} '
        f'Location="file://localhost{quote(str(audio))}">'
        f'<POSITION_MARK Name="Drop" Type="0" Start="30.0" Num="0"/></TRACK>'
        for idx, audio in enumerate(audios)
    ]
    body = "\n".join(parts)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<DJ_PLAYLISTS Version="1.0.0"><PRODUCT Name="Lexicon"/>
<COLLECTION Entries="{len(audios)}">
{body}
</COLLECTION></DJ_PLAYLISTS>"""


@pytest.mark.requirement("DJIMPORT-10053")
def test_rekordbox_xml_fills_genre_and_counts_what_it_carries_but_does_not_store(state_conn, tmp_path: Path) -> None:
    """[if] rekordbox XML carries genre and other extensions [then] genre fills and the rest are counted, [else stop]."""
    attrs = 'Genre="Invented House" Label="Invented Records" Year="2019" DateAdded="2026-11-08"'
    audios = [_wav(tmp_path / "music" / f"Invented XML Tune {i + 1}.wav") for i in range(3)]
    xml = tmp_path / "export.xml"
    xml.write_text(_rb_xml_tracks(audios, attrs))
    writer = StateWriter(state_conn, bus=FakeEventBus(), actor="seed")
    try:
        writer.upsert_track(
            stable_id="preseed-xml-genre",
            stable_id_tier="inferred",
            title="Invented XML Tune 1",
            artists=[],
            album=None,
            isrc=None,
            duration_ms=None,
            file_path=str(audios[0]),
        )
        writer.set_field("preseed-xml-genre", "genre", "Invented Techno", source="webui", modified_at=_STAMP)
    finally:
        writer.close()

    report = _ingest_xml(state_conn, xml)

    assert report.fields_written["genre"] == 2
    assert report.fields_kept["genre"] == 1
    assert _fields(state_conn, _sid_for(state_conn, audios[1]))["genre"] == ("Invented House", "rekordbox")
    assert _fields(state_conn, _sid_for(state_conn, audios[2]))["genre"] == ("Invented House", "rekordbox")
    assert _fields(state_conn, _sid_for(state_conn, audios[0]))["genre"] == ("Invented Techno", "webui")
    assert report.extensions_carried == {"label": 3, "year": 3, "date_added": 3}


@pytest.mark.requirement("DJIMPORT-10053")
def test_the_command_line_summary_prints_genre_and_carried_not_stored_counts(
    state_db_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] rekordbox-xml is imported from the CLI [then] the summary names genre and carried counts, [else stop]."""
    attrs = 'Genre="Invented House" Label="Invented Records" Year="2019"'
    a1 = _wav(tmp_path / "music" / "one.wav")
    a2 = _wav(tmp_path / "music" / "two.wav")
    xml = tmp_path / "export.xml"
    xml.write_text(_rb_xml_tracks([a1, a2], attrs))
    assert vendor.main(["rekordbox-xml", "--source", str(xml), "--db", str(state_db_path), "--write"]) == 0
    out = capsys.readouterr().out
    genre_line = next(line for line in out.splitlines() if line.strip().startswith("genre"))
    assert "written 2" in genre_line
    assert "carried, not stored:  label 2, year 2" in out


@pytest.mark.requirement("DJIMPORT-10053")
def test_serato_and_traktor_imports_carry_nothing_extra(
    state_conn, state_db_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] Traktor is ingested [then] nothing is carried as an unstored extension, [else stop]."""
    audio = _wav(tmp_path / "music" / "Invented Traktor.wav")
    nml = tmp_path / "collection.nml"
    nml.write_text(_nml([_entry(audio, title="Invented Traktor", bpm="120.000000")]))
    report = _ingest_nml(state_conn, nml)
    assert report.extensions_carried == {}
    assert report.fields_written.get("genre", 0) == 0
    assert vendor.main(["traktor", "--source", str(nml), "--db", str(state_db_path)]) == 0
    assert "carried, not stored:  none" in capsys.readouterr().out


@pytest.mark.requirement("DJIMPORT-6039")
def test_a_serato_mp3_whose_tag_cannot_be_read_is_counted_not_silent(tmp_path: Path) -> None:
    """[if] a Serato track's MP3 has an ID3 tag that does not parse [then] the read counts its cues as
    dropped, and a readable MP3 adds nothing, [else stop]."""
    music = tmp_path / "music"
    music.mkdir()
    broken = music / "Invented Broken.mp3"
    # An ID3v2.3 header claiming a 4096-byte tag in a file that ends early.
    broken.write_bytes(b"ID3\x03\x00\x00\x00\x00\x20\x00" + b"\x00" * 16)
    clean = music / "Invented Clean.mp3"
    clean.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 16)  # no tag at all
    serato_dir = tmp_path / "_Serato_"
    drive = vendor.serato_volume_root(serato_dir)
    rel = lambda p: str(p.relative_to(drive))  # noqa: E731
    SeratoAdapter().write(
        OpenDjLibrary(
            version="0.1",
            tracks=(
                Track(track_id="s1", file_path=rel(broken), title="Invented Broken"),
                Track(track_id="s2", file_path=rel(clean), title="Invented Clean"),
            ),
            playlists=(),
        ),
        serato_dir,
    )
    read = vendor.read_vendor("serato", serato_dir)
    assert len(read.library.tracks) == 2
    assert read.reader_notes == {"cues dropped: Serato GEOB frames could not be read (Id3Error)": 1}


def _id3_with_markers2(payload: bytes, *, format_flags: int = 0, sealed_body: bytes | None = None) -> bytes:
    """An ID3v2.3 tag holding one ``Serato Markers2`` GEOB frame, then an MPEG frame header.

    ``sealed_body`` replaces the frame body as stored, for a frame whose
    ``format_flags`` mark it compressed or encrypted.
    """
    body = b"\x00" + b"application/octet-stream\x00" + b"\x00" + b"Serato Markers2\x00" + payload
    if sealed_body is not None:
        body = sealed_body
    frame = b"GEOB" + len(body).to_bytes(4, "big") + b"\x00" + bytes([format_flags]) + body
    size = len(frame)
    syncsafe = bytes(((size >> shift) & 0x7F) for shift in (21, 14, 7, 0))
    return b"ID3\x03\x00\x00" + syncsafe + frame + b"\xff\xfb\x90\x00" + b"\x00" * 16


@pytest.mark.requirement("DJIMPORT-6039")
def test_a_serato_markers2_frame_that_cannot_be_decoded_is_counted(tmp_path: Path) -> None:
    """[if] a Serato MP3's tag reads but its Markers2 frame cannot be decoded [then] its cues are
    counted as dropped, and a decodable empty frame adds nothing, [else stop]."""
    from apps.adapters.serato.geob import Markers2, encode_markers2, read_geob_frames

    music = tmp_path / "music"
    music.mkdir()
    garbled = music / "Invented Garbled.mp3"
    garbled.write_bytes(_id3_with_markers2(b"\x02\x09not a frame this codec knows"))
    decodable = music / "Invented Decodable.mp3"
    decodable.write_bytes(_id3_with_markers2(encode_markers2(Markers2())))
    # control: the tag itself reads in both files, so only the frame differs
    assert [name for name, _ in read_geob_frames(garbled, strict=True).markers2.unknown_tags] == ["__raw__"]
    assert read_geob_frames(decodable, strict=True).markers2.unknown_tags == ()
    serato_dir = tmp_path / "_Serato_"
    drive = vendor.serato_volume_root(serato_dir)
    rel = lambda p: str(p.relative_to(drive))  # noqa: E731
    SeratoAdapter().write(
        OpenDjLibrary(
            version="0.1",
            tracks=(
                Track(track_id="s1", file_path=rel(garbled), title="Invented Garbled"),
                Track(track_id="s2", file_path=rel(decodable), title="Invented Decodable"),
            ),
        ),
        serato_dir,
    )
    read = vendor.read_vendor("serato", serato_dir)
    assert len(read.library.tracks) == 2
    assert read.reader_notes == {"cues dropped: Serato Markers2 frame could not be decoded": 1}


@pytest.mark.requirement("DJIMPORT-6039")
def test_a_serato_markers2_cue_too_short_to_read_is_counted(tmp_path: Path) -> None:
    """[if] a decodable Markers2 frame holds a CUE entry too short for its position [then] that cue is
    counted as dropped, and a whole CUE adds nothing, [else stop]."""
    from apps.adapters.serato.geob import Markers2, Markers2Cue, encode_markers2, read_geob_frames

    music = tmp_path / "music"
    music.mkdir()
    short = music / "Invented Short Cue.mp3"
    short.write_bytes(_id3_with_markers2(encode_markers2(Markers2(unknown_tags=(("CUE", b"\x00" * 5),)))))
    whole = music / "Invented Whole Cue.mp3"
    whole.write_bytes(_id3_with_markers2(encode_markers2(Markers2(
        cues=(Markers2Cue(index=0, position_ms=1000, color_rgb=0xCC0000, name="", kind="hot"),),
    ))))
    # control: the short entry survives decoding as an unread CUE, the whole one as a cue
    assert [name for name, _ in read_geob_frames(short, strict=True).markers2.unknown_tags] == ["CUE"]
    assert [c.position_ms for c in read_geob_frames(whole, strict=True).markers2.cues] == [1000]
    serato_dir = tmp_path / "_Serato_"
    drive = vendor.serato_volume_root(serato_dir)
    rel = lambda p: str(p.relative_to(drive))  # noqa: E731
    SeratoAdapter().write(
        OpenDjLibrary(
            version="0.1",
            tracks=(
                Track(track_id="s1", file_path=rel(short), title="Invented Short Cue"),
                Track(track_id="s2", file_path=rel(whole), title="Invented Whole Cue"),
            ),
        ),
        serato_dir,
    )
    read = vendor.read_vendor("serato", serato_dir)
    assert read.reader_notes == {"cues dropped: Serato Markers2 CUE entry too short to read": 1}


@pytest.mark.requirement("DJIMPORT-6039")
def test_a_serato_markers2_frame_that_ends_inside_an_entry_is_counted(tmp_path: Path) -> None:
    """[if] a Markers2 entry declares more bytes than its frame holds, or the frame is empty, [then]
    the track's cues are counted as dropped, and a whole frame adds nothing, [else stop]."""
    import base64
    import struct

    from apps.adapters.serato.geob import Markers2, Markers2Cue, encode_markers2, read_geob_frames

    whole_frame = encode_markers2(Markers2(
        cues=(Markers2Cue(index=0, position_ms=1000, color_rgb=0xCC0000, name="", kind="hot"),),
    ))
    # a CUE entry whose declared length runs past the end of the body
    body = b"CUE\x00" + struct.pack(">I", 64) + b"\x00" * 8
    cut_frame = b"\x01\x01" + base64.b64encode(b"\x01\x01" + body).rstrip(b"=")
    music = tmp_path / "music"
    music.mkdir()
    cut = music / "Invented Cut Frame.mp3"
    cut.write_bytes(_id3_with_markers2(cut_frame))
    empty = music / "Invented Empty Frame.mp3"
    empty.write_bytes(_id3_with_markers2(b""))  # not even the 01 01 header
    # header only: the outer 01 01, or an inner 01 01 with no entry list or
    # NUL, or a whole BPMLOCK entry with no NUL after it
    no_end = b"BPMLOCK\x00" + struct.pack(">I", 1) + b"\x00"
    unended = {
        "Invented Outer Header Only": b"\x01\x01",
        "Invented Inner Header Only": b"\x01\x01" + base64.b64encode(b"\x01\x01").rstrip(b"="),
        "Invented No Terminator": b"\x01\x01" + base64.b64encode(b"\x01\x01" + no_end).rstrip(b"="),
    }
    for title, frame in unended.items():
        (music / f"{title}.mp3").write_bytes(_id3_with_markers2(frame))
    whole = music / "Invented Whole Frame.mp3"
    whole.write_bytes(_id3_with_markers2(whole_frame))
    # control: the parser flags the cut and empty frames and not the whole one
    assert read_geob_frames(cut, strict=True).markers2.truncated is True
    assert read_geob_frames(empty, strict=True).markers2.truncated is True
    for title in unended:
        assert read_geob_frames(music / f"{title}.mp3", strict=True).markers2.truncated is True, title
    assert read_geob_frames(whole, strict=True).markers2.truncated is False
    serato_dir = tmp_path / "_Serato_"
    drive = vendor.serato_volume_root(serato_dir)
    rel = lambda p: str(p.relative_to(drive))  # noqa: E731
    SeratoAdapter().write(
        OpenDjLibrary(
            version="0.1",
            tracks=(
                Track(track_id="s1", file_path=rel(cut), title="Invented Cut Frame"),
                Track(track_id="s2", file_path=rel(whole), title="Invented Whole Frame"),
                Track(track_id="s3", file_path=rel(empty), title="Invented Empty Frame"),
                *(
                    Track(track_id=f"u{n}", file_path=rel(music / f"{title}.mp3"), title=title)
                    for n, title in enumerate(unended)
                ),
            ),
        ),
        serato_dir,
    )
    read = vendor.read_vendor("serato", serato_dir)
    assert read.reader_notes == {
        "cues dropped: Serato Markers2 frame ends inside an entry": 1,
        "cues dropped: Serato Markers2 frame is empty": 1,
        "cues dropped: Serato Markers2 frame has no entry list": 2,
        "cues dropped: Serato Markers2 frame is missing its entry-list terminator": 1,
    }


@pytest.mark.requirement("DJIMPORT-6039")
def test_a_sealed_serato_geob_frame_is_counted(tmp_path: Path) -> None:
    """[if] an MP3's GEOB frame is encrypted, so it is never decoded, [then] the track's cues are
    counted as dropped, and a plain frame adds nothing, [else stop]."""
    from apps.adapters.serato.geob import Markers2, encode_markers2, read_geob_frames

    music = tmp_path / "music"
    music.mkdir()
    sealed = music / "Invented Sealed Frame.mp3"
    # ID3v2.3 encryption flag (0x40): the body opens with a method byte
    sealed.write_bytes(_id3_with_markers2(b"", format_flags=0x40, sealed_body=b"\x80" + b"\x5a" * 40))
    plain = music / "Invented Plain Frame.mp3"
    plain.write_bytes(_id3_with_markers2(encode_markers2(Markers2())))
    # control: the tag reads in both files; only the sealed one has a frame the reader skips
    assert read_geob_frames(sealed, strict=True).sealed_geob_frames == 1
    assert read_geob_frames(plain, strict=True).sealed_geob_frames == 0
    serato_dir = tmp_path / "_Serato_"
    drive = vendor.serato_volume_root(serato_dir)
    rel = lambda p: str(p.relative_to(drive))  # noqa: E731
    SeratoAdapter().write(
        OpenDjLibrary(
            version="0.1",
            tracks=(
                Track(track_id="s1", file_path=rel(sealed), title="Invented Sealed Frame"),
                Track(track_id="s2", file_path=rel(plain), title="Invented Plain Frame"),
            ),
        ),
        serato_dir,
    )
    read = vendor.read_vendor("serato", serato_dir)
    assert len(read.library.tracks) == 2
    assert read.reader_notes == {"cues dropped: ID3 GEOB frame is compressed or encrypted": 1}


def _id3_with_markers2_frames(*payloads: bytes) -> bytes:
    """An ID3v2.3 tag repeating the ``Serato Markers2`` GEOB frame, once per payload."""
    frames = b""
    for payload in payloads:
        body = b"\x00" + b"application/octet-stream\x00" + b"\x00" + b"Serato Markers2\x00" + payload
        frames += b"GEOB" + len(body).to_bytes(4, "big") + b"\x00\x00" + body
    syncsafe = bytes(((len(frames) >> shift) & 0x7F) for shift in (21, 14, 7, 0))
    return b"ID3\x03\x00\x00" + syncsafe + frames + b"\xff\xfb\x90\x00" + b"\x00" * 16


@pytest.mark.requirement("DJIMPORT-04")
def test_a_serato_file_that_cannot_be_read_stops_the_import_rather_than_being_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] an existing Serato audio file cannot be read for a filesystem reason [then] the read raises,
    so the import stops, and it is not counted as a lost tag, [else stop]."""
    from apps.adapters.serato import adapter as serato_adapter
    from apps.open_dj import AdapterReport

    def unreadable(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(13, "Invented permission denied")

    monkeypatch.setattr(serato_adapter, "read_geob_frames", unreadable)
    music = tmp_path / "music"
    music.mkdir()
    locked = music / "Invented Locked.mp3"
    locked.write_bytes(b"ID3")
    with pytest.raises(PermissionError):
        serato_adapter.SeratoAdapter()._read_geob_for_track(str(locked), "s1", AdapterReport())


@pytest.mark.requirement("DJIMPORT-6039")
def test_a_lossy_markers2_frame_shadowed_by_a_later_one_is_counted(tmp_path: Path) -> None:
    """[if] an earlier, undecodable copy of a repeated Markers2 frame [then] its loss is counted, and two good copies add nothing, [else stop]."""
    from apps.adapters.serato.geob import Markers2, Markers2Cue, encode_markers2, read_geob_frames

    good = encode_markers2(Markers2(cues=(Markers2Cue(index=0, position_ms=1000, color_rgb=0xCC0000, name=""),)))
    music = tmp_path / "music"
    music.mkdir()
    shadowed = music / "Invented Shadowed Frame.mp3"
    shadowed.write_bytes(_id3_with_markers2_frames(b"\x02\x09not a frame this codec knows", good))
    doubled = music / "Invented Doubled Frame.mp3"
    doubled.write_bytes(_id3_with_markers2_frames(good, good))
    # control: both tags read, and the last Markers2 copy in each decodes with its cue
    for path in (shadowed, doubled):
        bundle = read_geob_frames(path, strict=True)
        assert [c.position_ms for c in bundle.markers2.cues] == [1000]
        assert len(bundle.shadowed_markers2) == 1
    serato_dir = tmp_path / "_Serato_"
    drive = vendor.serato_volume_root(serato_dir)
    rel = lambda p: str(p.relative_to(drive))  # noqa: E731
    SeratoAdapter().write(
        OpenDjLibrary(
            version="0.1",
            tracks=(
                Track(track_id="s1", file_path=rel(shadowed), title="Invented Shadowed Frame"),
                Track(track_id="s2", file_path=rel(doubled), title="Invented Doubled Frame"),
            ),
        ),
        serato_dir,
    )
    read = vendor.read_vendor("serato", serato_dir)
    assert len(read.library.tracks) == 2
    assert read.reader_notes == {"cues dropped: Serato Markers2 frame could not be decoded": 1}


@pytest.mark.requirement("DJIMPORT-10062")
def test_serato_flac_mp4_and_aiff_tracks_keep_their_cues_and_count_no_loss(
    state_conn, state_db_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] Serato lossless files carry Markers2 [then] import keeps cues with zero reader losses, [else stop]."""
    from apps.adapters.serato.geob import GEOB_BEATGRID_DESC, GEOB_MARKERS2_DESC
    from tests.fixtures import serato_containers as sc

    music = tmp_path / "music"
    music.mkdir()
    m2 = sc.markers2_payload()
    bg = sc.beatgrid_payload()
    text_m2 = sc.serato_text(GEOB_MARKERS2_DESC, m2)
    text_bg = sc.serato_text(GEOB_BEATGRID_DESC, bg)
    flac = music / "Invented Lossless.flac"
    sc.write_flac(flac, [(sc.FLAC_MARKERS2_KEY, text_m2), (sc.FLAC_BEATGRID_KEY, text_bg)])
    m4a = music / "Invented Lossless.m4a"
    sc.write_mp4(
        m4a,
        [
            (sc.MP4_SERATO_MEAN, sc.MP4_MARKERS2_NAME, text_m2.encode("ascii")),
            (sc.MP4_SERATO_MEAN, sc.MP4_BEATGRID_NAME, text_bg.encode("ascii")),
        ],
    )
    aiff = music / "Invented Lossless.aiff"
    sc.write_aiff_with_id3(aiff, m2, bg)
    serato_dir = tmp_path / "_Serato_"
    drive = vendor.serato_volume_root(serato_dir)
    rel = lambda p: str(p.relative_to(drive))  # noqa: E731
    SeratoAdapter().write(
        OpenDjLibrary(
            version="0.1",
            tracks=(
                Track(track_id="s1", file_path=rel(flac), title="Invented Lossless FLAC"),
                Track(track_id="s2", file_path=rel(m4a), title="Invented Lossless M4A"),
                Track(track_id="s3", file_path=rel(aiff), title="Invented Lossless AIFF"),
            ),
        ),
        serato_dir,
    )
    read = vendor.read_vendor("serato", serato_dir)
    assert read.reader_notes == {}
    for track in read.library.tracks:
        hot = [c for c in track.cues if c.type == "hot"]
        loops = [c for c in track.cues if c.type == "loop"]
        assert len(hot) == 2
        assert len(loops) == 1
        assert track.beats
    assert vendor.main(["serato", "--source", str(serato_dir), "--db", str(state_db_path), "--write"]) == 0
    assert "not carried over by the reader: 0" in capsys.readouterr().out
    for title in ("Invented Lossless FLAC", "Invented Lossless M4A", "Invented Lossless AIFF"):
        row = state_conn.execute("SELECT stable_id FROM tracks WHERE title = ?", (title,)).fetchone()
        assert row is not None
        blob, _ = cue_store.read_row(state_conn, str(row[0]))
        assert blob


@pytest.mark.requirement("DJIMPORT-10062")
def test_a_serato_flac_comment_that_is_not_base64_is_counted_with_its_file_name(tmp_path: Path) -> None:
    """[if] one FLAC comment is invalid base64 [then] reader_notes name the file once, [else stop]."""
    from tests.fixtures import serato_containers as sc

    music = tmp_path / "music"
    music.mkdir()
    bad = music / "Invented Bad Comment.flac"
    sc.write_flac(bad, [(sc.FLAC_MARKERS2_KEY, "not base64!!")])
    good = music / "Invented Good Comment.flac"
    m2 = sc.markers2_payload()
    from apps.adapters.serato.geob import GEOB_MARKERS2_DESC

    sc.write_flac(good, [(sc.FLAC_MARKERS2_KEY, sc.serato_text(GEOB_MARKERS2_DESC, m2))])
    serato_dir = tmp_path / "_Serato_"
    drive = vendor.serato_volume_root(serato_dir)
    rel = lambda p: str(p.relative_to(drive))  # noqa: E731
    SeratoAdapter().write(
        OpenDjLibrary(
            version="0.1",
            tracks=(
                Track(track_id="s1", file_path=rel(bad), title="Invented Bad Comment"),
                Track(track_id="s2", file_path=rel(good), title="Invented Good Comment"),
            ),
        ),
        serato_dir,
    )
    read = vendor.read_vendor("serato", serato_dir)
    notes = read.reader_notes
    assert len(notes) == 1
    key = next(iter(notes))
    assert key.startswith("cues dropped: Serato tags could not be read:")
    assert "Invented Bad Comment.flac" in key
    assert notes[key] == 1
    good_track = next(t for t in read.library.tracks if t.title == "Invented Good Comment")
    assert len([c for c in good_track.cues if c.type == "hot"]) == 2


@pytest.mark.requirement("DJIMPORT-6039")
def test_a_djay_cue_list_that_stops_parsing_is_counted() -> None:
    """[if] a djay cue or loop array names more elements than parse, or holds one with no usable
    position, [then] it is counted as unreadable, and a whole one is not, [else stop]."""
    import struct

    from apps.shared.djay_db import parse_cues_with_loss
    from apps.shared.normalised import NormalisedCue
    from apps.sync.djay_writer import encode_cue_array

    def blob(array: bytes) -> bytes:
        return b"TSAF" + b"\x00" * 12 + b"\x08cuePoints\x00" + array + b"\x00"

    array = encode_cue_array([
        NormalisedCue(position_msec=1000, kind="hot", index=0),
        NormalisedCue(position_msec=2000, kind="hot", index=1),
    ])
    cues, lost = parse_cues_with_loss(blob(array))
    assert (len(cues), lost) == (2, 0)  # control: a whole array reads in full
    overcounted = b"\x0b" + struct.pack("<I", 3) + array[5:]  # says 3, holds 2
    assert parse_cues_with_loss(blob(overcounted))[1] == 1
    assert parse_cues_with_loss(blob(b"\x05"))[1] == 1  # named, but not an array
    assert array.count(b"position") == 2
    no_position = array.replace(b"position", b"positiom", 1)  # first cue walks but has no position
    cues, lost = parse_cues_with_loss(blob(no_position))
    assert ([c.position_msec for c in cues], lost) == ([2000], 1)
    one_second = struct.pack("<f", 1.0)
    assert array.count(one_second) == 1
    # a non-finite position, a negative one (even one that rounds to zero), or a float32 too large to store in ms,
    # is rejected, not raised
    for bad in (float("inf"), float("-inf"), float("nan"), 3.0e38, -1.0, -0.0004):
        cues, lost = parse_cues_with_loss(blob(array.replace(one_second, struct.pack("<f", bad))))
        assert ([c.position_msec for c in cues], lost) == ([2000], 1)
    assert parse_cues_with_loss(b"TSAF" + b"\x00" * 12)[1] == 0  # no cue list at all


@pytest.mark.requirement("DJIMPORT-6039")
def test_a_djay_cue_array_cut_short_inside_its_last_element_is_counted() -> None:
    """[if] a djay cue array ends inside its last element, before the element terminator [then] the array is counted as
    unreadable, while a whole array is not, [else stop]."""
    from apps.shared.djay_db import parse_cues_with_loss
    from apps.shared.normalised import NormalisedCue
    from apps.sync.djay_writer import encode_cue_array

    array = encode_cue_array([
        NormalisedCue(position_msec=1000, kind="hot", index=0),
        NormalisedCue(position_msec=2000, kind="hot", index=1),
    ])

    def blob(arr: bytes) -> bytes:
        return b"TSAF" + b"\x00" * 12 + b"\x08cuePoints\x00" + arr

    assert parse_cues_with_loss(blob(array))[1] == 0  # control: a whole array reads in full
    for cut in range(1, 8):  # the element terminator, then the key and value bytes before it
        assert parse_cues_with_loss(blob(array[:-cut]))[1] == 1, cut


@pytest.mark.requirement("DJIMPORT-6039")
def test_a_djay_loop_whose_length_cannot_be_read_is_a_lost_loop() -> None:
    """[if] a djay loop has a length that is not a positive finite number the store can hold [then] the loop is
    counted as unreadable, not stored without a length, [else stop]."""
    from apps.shared.djay_db import _CUE_CLASS, _LOOP_CLASS, _fields_to_cue

    loop = {"position": 1.0, "class": _LOOP_CLASS.decode("utf-8")}
    assert _fields_to_cue({**loop, "length": 2.5}).loop_length_msec == 2500  # control: a readable length is kept
    for bad in ("abc", float("nan"), float("inf"), 0.0, -1.0, -0.0004, 0.0004, 1e300):
        assert _fields_to_cue({**loop, "length": bad}) is None, bad
    assert _fields_to_cue(loop) is None  # a loop with no length at all is lost too
    cue = {"position": 1.0, "class": _CUE_CLASS.decode("utf-8"), "index": 0}
    assert _fields_to_cue(cue).kind == "hot"  # control: the cue class reads
    assert _fields_to_cue({**cue, "class": "ADCMediaItemSomethingElse"}) is None  # an unknown class is no cue


@pytest.mark.requirement("DJIMPORT-6039")
def test_each_sealed_serato_geob_frame_in_one_tag_is_counted(tmp_path: Path) -> None:
    """[if] one MP3's tag holds two encrypted GEOB frames [then] each is counted as a dropped loss, [else stop]."""
    from apps.adapters.serato.geob import read_geob_frames

    sealed_body = b"\x80" + b"\x5a" * 40
    frame = b"GEOB" + len(sealed_body).to_bytes(4, "big") + b"\x00" + bytes([0x40]) + sealed_body
    frames = frame + frame
    size = len(frames)
    syncsafe = bytes(((size >> shift) & 0x7F) for shift in (21, 14, 7, 0))
    music = tmp_path / "music"
    music.mkdir()
    two = music / "Invented Two Sealed Frames.mp3"
    two.write_bytes(b"ID3\x03\x00\x00" + syncsafe + frames + b"\xff\xfb\x90\x00" + b"\x00" * 16)
    # control: the tag reads, and both frames are seen as sealed
    assert read_geob_frames(two, strict=True).sealed_geob_frames == 2
    serato_dir = tmp_path / "_Serato_"
    drive = vendor.serato_volume_root(serato_dir)
    rel = str(two.relative_to(drive))
    SeratoAdapter().write(
        OpenDjLibrary(
            version="0.1",
            tracks=(Track(track_id="s1", file_path=rel, title="Invented Two Sealed Frames"),),
        ),
        serato_dir,
    )
    read = vendor.read_vendor("serato", serato_dir)
    assert read.reader_notes == {"cues dropped: ID3 GEOB frame is compressed or encrypted": 2}


def test_only_the_djimport_6039_loss_kinds_are_counted() -> None:
    """[if] an adapter warns about a kind DJIMPORT-6039 does not list [then] it is not counted, [else stop]."""
    from apps.open_dj.schema import AdapterReport

    report = AdapterReport()
    report.warn(field="bpm", track_id="1", action="dropped", reason="unparseable BPM 'x'")
    report.warn(field="playlist_entry", track_id="1", action="dropped", reason="crate has no row")
    report.warn(field="cues", track_id="1", action="dropped", reason="ID3 GEOB frame is compressed or encrypted")

    assert vendor._count_warnings(report, vendor._SERATO_READER_LOSSES) == {
        "bpm dropped: unparseable BPM 'x'": 1,
        "cues dropped: ID3 GEOB frame is compressed or encrypted": 1,
    }


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


def _serato32(value: int) -> bytes:
    """serato-tags' documented serato32 encoder, the inverse of the codec's decoder."""
    a, b, c = (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF
    return bytes((a >> 5, ((b >> 6) | (a << 2)) & 0x7F, ((c >> 7) | (b << 1)) & 0x7F, c & 0x7F))


def _serato_markers_legacy(cue0_ms: int, cue0_rgb: int) -> bytes:
    """A ``Serato Markers_`` payload that sets hot cue 0 only; every other slot is unset."""
    unset_start_end = b"\x7f" * 10
    set_cue = b"\x00" + _serato32(cue0_ms) + b"\x7f" * 5 + b"\x00\x7f\x7f\x7f\x7f\x7f" + _serato32(cue0_rgb) + bytes((1, 0))
    unset_cue = unset_start_end + b"\x00\x7f\x7f\x7f\x7f\x7f" + _serato32(0) + bytes((1, 0))
    unset_loop = unset_start_end + b"\x00\x7f\x7f\x7f\x7f\x7f" + _serato32(0) + bytes((3, 0))
    entries = [set_cue] + [unset_cue] * 4 + [unset_loop] * 9
    return b"\x02\x05" + (14).to_bytes(4, "big") + b"".join(entries) + b"\x07\x7f\x7f\x7f"


def _id3_with_serato_geob_frames(frames: tuple[tuple[str, bytes], ...]) -> bytes:
    """An ID3v2.3 tag holding one GEOB frame per (description, payload), then an MPEG frame header."""
    body_frames = b""
    for desc, payload in frames:
        body = b"\x00" + b"application/octet-stream\x00" + b"\x00" + desc.encode() + b"\x00" + payload
        body_frames += b"GEOB" + len(body).to_bytes(4, "big") + b"\x00\x00" + body
    size = len(body_frames)
    syncsafe = bytes(((size >> shift) & 0x7F) for shift in (21, 14, 7, 0))
    return b"ID3\x03\x00\x00" + syncsafe + body_frames + b"\xff\xfb\x90\x00" + b"\x00" * 16


@pytest.mark.requirement("DJIMPORT-6039")
def test_serato_cues_survive_read_and_markers_legacy_overrides_markers2_position(tmp_path: Path) -> None:
    """[if] a Serato MP3 holds Markers2 and Markers_ [then] the cue Markers_ sets takes its position and
    the cue is kept, and a file with Markers2 alone keeps its cue, [else stop]."""
    from apps.adapters.serato.geob import Markers2, Markers2Cue, encode_markers2

    music = tmp_path / "music"
    music.mkdir()
    markers2 = encode_markers2(Markers2(cues=(
        Markers2Cue(index=0, position_ms=1_000, color_rgb=0xCC0000, name="Intro", kind="hot"),
    )))
    both = music / "Invented Legacy Wins.mp3"
    both.write_bytes(_id3_with_serato_geob_frames((
        ("Serato Markers2", markers2),
        ("Serato Markers_", _serato_markers_legacy(1_500, 0xCC8800)),
    )))
    alone = music / "Invented Markers2 Only.mp3"
    alone.write_bytes(_id3_with_serato_geob_frames((("Serato Markers2", markers2),)))
    serato_dir = tmp_path / "_Serato_"
    drive = vendor.serato_volume_root(serato_dir)
    rel = lambda p: str(p.relative_to(drive))  # noqa: E731
    SeratoAdapter().write(
        OpenDjLibrary(
            version="0.1",
            tracks=(
                Track(track_id="s1", file_path=rel(both), title="Invented Legacy Wins"),
                Track(track_id="s2", file_path=rel(alone), title="Invented Markers2 Only"),
            ),
        ),
        serato_dir,
    )
    read = vendor.read_vendor("serato", serato_dir)
    by_title = {t.title: t for t in read.library.tracks}
    assert [(c.name, c.position_ms) for c in by_title["Invented Legacy Wins"].cues] == [("Intro", 1_500)]
    assert [(c.name, c.position_ms) for c in by_title["Invented Markers2 Only"].cues] == [("Intro", 1_000)]


@pytest.mark.requirement("DJIMPORT-10056")
def test_virtualdj_cli_dry_run_and_write(tmp_path: Path, capsys) -> None:
    """[if] virtualdj import runs on a tmp library [then] dry-run leaves tracks empty and write stores tracks without track_fields, [else stop]."""
    from apps.shared.state import db as state_db

    vdj_dir = tmp_path / "vdj"
    vdj_dir.mkdir()
    a = _wav(vdj_dir / "a.wav")
    b = _wav(vdj_dir / "b.wav")
    missing = "/Volumes/Invented DJ/Music/ghost.wav"
    (vdj_dir / "database.xml").write_text(
        '<?xml version="1.0"?><VirtualDJ_Database>'
        f'<Song FilePath="{a}"><Tags Author="A" Title="A" Album="A" Key="Am" Stars="3" Bpm="0.5"/>'
        f'<Infos SongLength="1.0" PlayCount="1"/>'
        f'<Poi Pos="0.1" Type="cue" Num="1"/></Song>'
        f'<Song FilePath="{b}"><Tags Title="B"/><Infos SongLength="2.0"/></Song>'
        "</VirtualDJ_Database>",
        encoding="utf-8",
    )
    (vdj_dir / "MyLists").mkdir()
    (vdj_dir / "MyLists" / "Mix.vdjfolder").write_text(
        f'<?xml version="1.0"?><VirtualFolder>'
        f'<song path="{b}"/><song path="{missing}"/></VirtualFolder>',
        encoding="utf-8",
    )
    state_path = tmp_path / "state.db"
    assert vendor.run_cli(argparse.Namespace(source=str(vdj_dir), write=False, db=str(state_path)), "virtualdj") == 0
    out = capsys.readouterr().out
    assert "virtualdj import (dry-run)" in out
    conn_dry = state_db.open_rw(state_path)
    try:
        assert conn_dry.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 0
    finally:
        conn_dry.close()

    assert vendor.run_cli(argparse.Namespace(source=str(vdj_dir), write=True, db=str(state_path)), "virtualdj") == 0
    out = capsys.readouterr().out
    assert "read, not stored yet:" in out
    assert "bpm" in out and "cues" in out
    assert "playlist entries unresolved: 1" in out
    conn = state_db.open_rw(state_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM track_fields").fetchone()[0] == 0
        vendors = {r[0] for r in conn.execute("SELECT DISTINCT vendor FROM track_vendor_ids")}
        assert vendors == {"virtualdj"}
        (pl_id,) = conn.execute("SELECT playlist_id FROM playlists WHERE vendor='virtualdj'").fetchone()
        members = [r[0] for r in conn.execute(
            "SELECT stable_id FROM playlist_memberships WHERE playlist_id=? ORDER BY order_key",
            (pl_id,),
        )]
        assert len(members) == 1
    finally:
        conn.close()


@pytest.mark.requirement("DJIMPORT-10056")
def test_virtualdj_missing_source_returns_one(capsys) -> None:
    """[if] virtualdj has no default source [then] CLI returns 1, [else stop]."""
    assert vendor.run_cli(argparse.Namespace(source=None, write=False, db=None), "virtualdj") == 1


@pytest.mark.requirement("DJIMPORT-10056")
def test_virtualdj_default_source(monkeypatch, tmp_path: Path) -> None:
    """[if] database.xml lives under Documents/VirtualDJ [then] default_source finds it, [else stop]."""
    home = tmp_path / "home"
    vdj = home / "Documents" / "VirtualDJ"
    vdj.mkdir(parents=True)
    (vdj / "database.xml").write_text(
        '<?xml version="1.0"?><VirtualDJ_Database></VirtualDJ_Database>', encoding="utf-8"
    )
    monkeypatch.setattr(Path, "home", lambda: home)
    assert vendor.default_source("virtualdj") == vdj
    assert vendor.default_source("virtualdj") is not None
    (vdj / "database.xml").unlink()
    assert vendor.default_source("virtualdj") is None


@pytest.mark.requirement("DJIMPORT-10056")
def test_virtualdj_windows_path_ingest_missing_file(state_conn, tmp_path: Path) -> None:
    """[if] a Windows-path track is ingested on Linux [then] files_missing is 1 and path is verbatim, [else stop]."""
    win_path = r"D:\Music\Invented Tune Three.mp3"
    library = OpenDjLibrary(
        version="0.1",
        tracks=(Track(track_id="vdj_x", file_path=win_path, title="Three", artists=("A",), album=""),),
    )
    from apps.adapters.virtualdj import virtualdj_path_resolver

    writer = StateWriter(state_conn, bus=FakeEventBus(), actor="vdj-test")
    try:
        report = vendor.ingest_library(
            writer,
            library,
            vendor="virtualdj",
            source=str(tmp_path),
            resolve_path=virtualdj_path_resolver(windows=False),
            dry_run=False,
            modified_at=_STAMP,
        )
    finally:
        writer.close()
    assert report.files_missing == 1
    row = state_conn.execute("SELECT file_path FROM tracks WHERE title='Three'").fetchone()
    assert row is not None and row[0] == win_path
