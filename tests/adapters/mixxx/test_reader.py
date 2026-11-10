"""Mixxx mixxxdb.sqlite reader and library import (DJIMPORT-10059).

[if] a Mixxx library is read [then] tracks, cues, playlists and crates assemble into one library, [else stop].
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from apps.adapters import library_import as vendor
from apps.adapters.mixxx import MIXXX_KEYS, mixxx_key_to_camelot, read_mixxx_library
from apps.adapters.mixxx.reader import DEFAULT_LIBRARY_CANDIDATES
from apps.open_dj import serialize_jcs
from apps.open_dj.validate import validate_document
from apps.open_dj.wire import library_to_wire_document
from apps.shared.state import cue_store
from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter

pytestmark = pytest.mark.requirement("DJIMPORT-10059")


def _create_mixxx_schema(conn: sqlite3.Connection, *, with_color: bool) -> None:
    conn.execute("CREATE TABLE track_locations (id INTEGER PRIMARY KEY, location TEXT, fs_deleted INTEGER)")
    lib_cols = (
        "id INTEGER PRIMARY KEY, title TEXT, artist TEXT, album TEXT, genre TEXT, "
        "year INTEGER, bpm REAL, key TEXT, rating INTEGER, duration REAL, timesplayed INTEGER, "
        "samplerate INTEGER, location INTEGER"
    )
    if with_color:
        lib_cols += ", color INTEGER, comment TEXT, mixxx_deleted INTEGER DEFAULT 0"
    else:
        lib_cols += ", mixxx_deleted INTEGER DEFAULT 0"
    conn.execute(f"CREATE TABLE library ({lib_cols})")
    cue_cols = (
        "id INTEGER PRIMARY KEY, track_id INTEGER, type INTEGER, position REAL, length REAL, hotcue INTEGER, label TEXT"
    )
    if with_color:
        cue_cols += ", color INTEGER"
    conn.execute(f"CREATE TABLE cues ({cue_cols})")
    conn.execute("CREATE TABLE Playlists (id INTEGER PRIMARY KEY, name TEXT, position INTEGER, hidden INTEGER)")
    conn.execute("CREATE TABLE PlaylistTracks (playlist_id INTEGER, track_id INTEGER, position INTEGER, id INTEGER)")
    conn.execute("CREATE TABLE crates (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("CREATE TABLE crate_tracks (crate_id INTEGER, track_id INTEGER)")


def _seed_default_fixture(conn: sqlite3.Connection, path: Path, *, with_color: bool) -> None:
    music = path.parent / "music"
    music.mkdir(exist_ok=True)
    paths = [str(music / f"t{i}.mp3") for i in (1, 2, 3)]
    for i, p in enumerate(paths, start=1):
        conn.execute(
            "INSERT INTO track_locations (id, location, fs_deleted) VALUES (?, ?, 0)",
            (i, p),
        )
    comment = "note one" if with_color else None
    color_one = 0xFF0000 if with_color else None
    default_tracks = [
        (1, "One", "Artist One", "Alb1", "Techno", 2020, 128.0, "8A", 4, 300.0, 10, color_one, 44100, comment, 1, 0),
        (2, "Two", "Artist Two", "Alb2", "House", 2021, 124.0, "1d", 3, 240.0, 5, None, 44100, None, 2, 0),
        (3, "Three", "Artist Three", "Alb3", "Dub", 2019, 130.0, "F#m", 5, 360.0, 2, None, 48000, None, 3, 0),
    ]
    if with_color:
        sql = (
            "INSERT INTO library (id, title, artist, album, genre, year, bpm, key, rating, "
            "duration, timesplayed, color, samplerate, comment, location, mixxx_deleted) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        )
        conn.executemany(sql, default_tracks)
    else:
        slim = [
            (
                row[0],
                row[1],
                row[2],
                row[3],
                row[4],
                row[5],
                row[6],
                row[7],
                row[8],
                row[9],
                row[10],
                row[12],
                row[14],
                row[15],
            )
            for row in default_tracks
        ]
        sql = (
            "INSERT INTO library (id, title, artist, album, genre, year, bpm, key, rating, "
            "duration, timesplayed, samplerate, location, mixxx_deleted) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        )
        conn.executemany(sql, slim)

    default_cues = [
        (1, 1, 1, 88200, None, 0, "Drop", color_one),
        (2, 1, 2, 44100, None, -1, "Load", None),
        (3, 2, 4, 176400, 352800, 1, "Loop", None),
        (4, 2, 1, 441000, None, 2, "Hot2", None),
        (5, 3, 1, 96000, None, 0, "Hot3", None),
        (6, 3, 4, 0, 192000, -1, "Loop3", None),
    ]
    if with_color:
        conn.executemany(
            "INSERT INTO cues (id, track_id, type, position, length, hotcue, label, color) VALUES (?,?,?,?,?,?,?,?)",
            default_cues,
        )
    else:
        conn.executemany(
            "INSERT INTO cues (id, track_id, type, position, length, hotcue, label) VALUES (?,?,?,?,?,?,?)",
            [row[:7] for row in default_cues],
        )
    conn.execute("INSERT INTO Playlists (id, name, position, hidden) VALUES (1, 'Set', 0, 0)")
    conn.execute("INSERT INTO Playlists (id, name, position, hidden) VALUES (2, 'Auto DJ', 1, 1)")
    conn.execute("INSERT INTO Playlists (id, name, position, hidden) VALUES (3, 'Log', 2, 2)")
    conn.executemany(
        "INSERT INTO PlaylistTracks (playlist_id, track_id, position, id) VALUES (?,?,?,?)",
        [(1, 3, 0, 1), (1, 1, 1, 2), (1, 2, 2, 3)],
    )
    conn.execute("INSERT INTO crates (id, name) VALUES (1, 'Techno')")
    conn.executemany("INSERT INTO crate_tracks (crate_id, track_id) VALUES (?, ?)", [(1, 1), (1, 3)])


def _fill_custom_tracks(
    conn: sqlite3.Connection,
    *,
    with_color: bool,
    tracks: list[dict],
    cues: list[dict] | None,
) -> None:
    for row in tracks:
        _insert_track(conn, row, with_color=with_color)
    if cues:
        for row in cues:
            _insert_cue(conn, row, with_color=with_color)


def _fill_optional_tables(
    conn: sqlite3.Connection,
    *,
    playlists: list[tuple] | None,
    playlist_tracks: list[tuple] | None,
    crates: list[tuple] | None,
    crate_tracks: list[tuple] | None,
) -> None:
    if playlists is not None:
        for row in playlists:
            conn.execute("INSERT INTO Playlists (id, name, position, hidden) VALUES (?, ?, ?, ?)", row)
    if playlist_tracks is not None:
        for row in playlist_tracks:
            conn.execute(
                "INSERT INTO PlaylistTracks (playlist_id, track_id, position, id) VALUES (?,?,?,?)",
                row,
            )
    if crates is not None:
        for row in crates:
            conn.execute("INSERT INTO crates (id, name) VALUES (?, ?)", row)
    if crate_tracks is not None:
        for row in crate_tracks:
            conn.execute("INSERT INTO crate_tracks (crate_id, track_id) VALUES (?, ?)", row)


def _make_db(
    path: Path,
    *,
    with_color: bool = True,
    tracks: list[dict] | None = None,
    cues: list[dict] | None = None,
    playlists: list[dict] | None = None,
    playlist_tracks: list[dict] | None = None,
    crates: list[dict] | None = None,
    crate_tracks: list[dict] | None = None,
) -> Path:
    conn = sqlite3.connect(path)
    try:
        _create_mixxx_schema(conn, with_color=with_color)
        if tracks is None:
            _seed_default_fixture(conn, path, with_color=with_color)
        else:
            _fill_custom_tracks(conn, with_color=with_color, tracks=tracks, cues=cues)
        _fill_optional_tables(
            conn,
            playlists=playlists,
            playlist_tracks=playlist_tracks,
            crates=crates,
            crate_tracks=crate_tracks,
        )
        conn.commit()
    finally:
        conn.close()
    return path


def _insert_track(conn: sqlite3.Connection, row: dict, *, with_color: bool) -> None:
    loc_id = row["id"]
    conn.execute(
        "INSERT INTO track_locations (id, location, fs_deleted) VALUES (?, ?, ?)",
        (loc_id, row["path"], row.get("fs_deleted", 0)),
    )
    if with_color:
        conn.execute(
            "INSERT INTO library (id, title, artist, album, genre, year, bpm, key, rating, "
            "duration, timesplayed, color, samplerate, comment, location, mixxx_deleted) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["id"],
                row.get("title", ""),
                row.get("artist", ""),
                row.get("album", ""),
                row.get("genre", ""),
                row.get("year"),
                row.get("bpm"),
                row.get("key"),
                row.get("rating"),
                row.get("duration"),
                row.get("timesplayed", 0),
                row.get("color"),
                row.get("samplerate", 44100),
                row.get("comment"),
                loc_id,
                row.get("mixxx_deleted", 0),
            ),
        )
    else:
        conn.execute(
            "INSERT INTO library (id, title, artist, album, genre, year, bpm, key, rating, "
            "duration, timesplayed, samplerate, location, mixxx_deleted) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["id"],
                row.get("title", ""),
                row.get("artist", ""),
                row.get("album", ""),
                row.get("genre", ""),
                row.get("year"),
                row.get("bpm"),
                row.get("key"),
                row.get("rating"),
                row.get("duration"),
                row.get("timesplayed", 0),
                row.get("samplerate", 44100),
                loc_id,
                row.get("mixxx_deleted", 0),
            ),
        )


def _insert_cue(conn: sqlite3.Connection, row: dict, *, with_color: bool) -> None:
    cue_vals = (
        row["id"],
        row["track_id"],
        row["type"],
        row["position"],
        row.get("length"),
        row.get("hotcue", -1),
        row.get("label", ""),
    )
    if with_color:
        conn.execute(
            "INSERT INTO cues (id, track_id, type, position, length, hotcue, label, color) VALUES (?,?,?,?,?,?,?,?)",
            (*cue_vals, row.get("color")),
        )
    else:
        conn.execute(
            "INSERT INTO cues (id, track_id, type, position, length, hotcue, label) VALUES (?,?,?,?,?,?,?)",
            cue_vals,
        )


def _standard_db(tmp_path: Path) -> Path:
    return _make_db(tmp_path / "mixxxdb.sqlite")


def test_three_tracks_with_two_cues_each_read_into_a_library(tmp_path: Path) -> None:
    """[if] the standard Mixxx fixture is read [then] three tracks carry their fields and cue positions in ms, [else stop]."""
    db = _standard_db(tmp_path)
    lib = read_mixxx_library(db).library
    assert len(lib.tracks) == 3
    by_id = {t.track_id: t for t in lib.tracks}
    t1 = by_id["1"]
    assert t1.title == "One"
    assert t1.artists == ("Artist One",)
    assert t1.album == "Alb1"
    assert t1.bpm == 128.0
    assert t1.key_camelot == "8A"
    assert t1.rating == 4
    assert t1.duration_ms == 300_000
    assert t1.play_count == 10
    assert t1.color_rgb == 0xFF0000
    assert t1.extensions.get("x_comment") == "note one"
    assert t1.extensions.get("x_mixxx_genre") == "Techno"
    hot_positions = sorted(c.position_ms for c in t1.cues if c.type == "hot")
    assert hot_positions == [1000]
    assert by_id["2"].key_camelot == "8B"
    assert by_id["3"].key_camelot == "11A"
    assert sorted(c.position_ms for c in by_id["2"].cues) == [2000, 5000]
    assert sorted(c.position_ms for c in by_id["3"].cues) == [0, 1000]


def test_loops_carry_their_length_in_ms(tmp_path: Path) -> None:
    """[if] Mixxx stores loop cues in samples [then] length_ms is converted at the track samplerate, [else stop]."""
    lib = read_mixxx_library(_standard_db(tmp_path)).library
    by_id = {t.track_id: t for t in lib.tracks}
    loops = [c for c in by_id["2"].cues if c.type == "loop"]
    assert len(loops) == 1 and loops[0].length_ms == 4000
    loops3 = [c for c in by_id["3"].cues if c.type == "loop"]
    assert len(loops3) == 1 and loops3[0].length_ms == 2000


def test_the_main_cue_is_kept_as_a_load_cue(tmp_path: Path) -> None:
    """[if] Mixxx has a type-2 main cue [then] it is a load cue on track one, [else stop]."""
    loads = [c for t in read_mixxx_library(_standard_db(tmp_path)).library.tracks for c in t.cues if c.type == "load"]
    assert len(loads) == 1
    assert loads[0].position_ms == 500


def test_fs_deleted_tracks_are_counted_and_left_out_of_playlists(tmp_path: Path) -> None:
    """[if] a file is fs_deleted in Mixxx [then] it is counted deleted and left out of playlists, [else stop]."""
    db = _make_db(tmp_path / "mixxxdb.sqlite")
    conn = sqlite3.connect(db)
    conn.execute("UPDATE track_locations SET fs_deleted = 1 WHERE id = 2")
    conn.commit()
    conn.close()
    read = vendor.read_vendor("mixxx", db)
    assert read.reader_notes.get("location dropped: deleted in Mixxx") == 1
    assert read.entries_dropped >= 1
    assert {t.track_id for t in read.library.tracks} == {"1", "3"}
    set_pl = next(p for p in read.library.playlists if p.name == "Set")
    assert set_pl.track_ids == ("3", "1")
    conn = state_db.open_rw(tmp_path / "state.db")
    writer = StateWriter(conn, bus=FakeEventBus(), actor="test")
    try:
        report = vendor.ingest_library(
            writer, read.library, vendor="mixxx", source=str(db), resolve_path=vendor._decoded_path, dry_run=False
        )
    finally:
        writer.close()
        conn.close()
    assert report.playlist_entries_unresolved == 0


def test_cli_summary_names_location_dropped_deleted_in_mixxx(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] fs_deleted tracks were skipped [then] the CLI summary names location dropped deleted in Mixxx, [else stop]."""
    db = _make_db(tmp_path / "mixxxdb.sqlite")
    conn = sqlite3.connect(db)
    conn.execute("UPDATE track_locations SET fs_deleted = 1 WHERE id = 2")
    conn.commit()
    conn.close()
    for t in read_mixxx_library(db).library.tracks:
        Path(t.file_path).write_bytes(b"")
    assert vendor.main(["mixxx", "--source", str(db), "--db", str(tmp_path / "state.db"), "--write"]) == 0
    out = capsys.readouterr().out
    assert "location dropped: deleted in Mixxx" in out


def test_playlists_keep_position_order_and_hidden_ones_are_skipped_and_counted(tmp_path: Path) -> None:
    """[if] playlists have hidden flags and out-of-order members [then] only Set remains in position order, [else stop]."""
    mixxx = read_mixxx_library(_standard_db(tmp_path))
    set_pl = next(p for p in mixxx.library.playlists if p.name == "Set")
    assert set_pl.track_ids == ("3", "1", "2")
    top_level = [p.name for p in mixxx.library.playlists if p.name != "Crates"]
    assert top_level == ["Set"]
    assert mixxx.notes.get("playlist dropped: hidden in Mixxx (Auto DJ or set log)") == 2


def test_crates_import_as_playlists_under_a_crates_folder(tmp_path: Path) -> None:
    """[if] Mixxx has crates [then] a Crates folder holds each crate as a child playlist, [else stop]."""
    lib = read_mixxx_library(_standard_db(tmp_path)).library
    crates_folder = next(p for p in lib.playlists if p.name == "Crates")
    assert crates_folder.track_ids == ()
    techno = next(c for c in crates_folder.children if c.name == "Techno")
    assert techno.track_ids == ("1", "3")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("8A", "8A"),
        ("12B", "12B"),
        ("1m", "8A"),
        ("6d", "1B"),
        ("12d", "7B"),
        ("Am", "8A"),
        ("F#m", "11A"),
        ("Bb", "6B"),
        ("D♭", "3B"),
        ("B♭m", "3A"),
        ("1d (C)", "8B"),
        ("", None),
        (None, None),
        ("Xq", None),
    ],
)
def test_mixxx_key_notations_map_to_camelot(raw: str | None, expected: str | None) -> None:
    """[if] Mixxx key text uses a known notation [then] Camelot is returned, [else stop]."""
    assert mixxx_key_to_camelot(raw) == expected


def test_open_key_column_follows_the_camelot_offset() -> None:
    """[if] MIXXX_KEYS is the authority [then] Open Key numbers follow Camelot+6 mod 12, [else stop]."""
    for camelot, open_key, _trad in MIXXX_KEYS:
        num = int(camelot[:-1])
        letter = camelot[-1]
        ok_num = int(open_key[:-1])
        ok_letter = open_key[-1]
        assert ((num + 4) % 12) + 1 == ok_num
        assert (letter, ok_letter) in (("A", "m"), ("B", "d"))


def test_unknown_key_in_db_counts_as_not_recognised(tmp_path: Path) -> None:
    """[if] a track key is unknown [then] the reader counts key dropped notation, [else stop]."""
    music = tmp_path / "music"
    music.mkdir()
    p = str(music / "x.mp3")
    db = _make_db(
        tmp_path / "mixxxdb.sqlite",
        tracks=[{"id": 1, "path": p, "key": "Xq", "samplerate": 44100, "title": "X"}],
        cues=[],
        playlists=[],
        playlist_tracks=[],
        crates=[],
        crate_tracks=[],
    )
    mixxx = read_mixxx_library(db)
    assert mixxx.notes.get("key dropped: notation not recognised") == 1
    assert mixxx.library.tracks[0].key_camelot is None


def test_unsupported_cue_types_and_missing_samplerate_are_counted(tmp_path: Path) -> None:
    """[if] a cue type or samplerate is unusable [then] the reader counts the drop, [else stop]."""
    music = tmp_path / "music"
    music.mkdir()
    db = _make_db(
        tmp_path / "mixxxdb.sqlite",
        tracks=[
            {"id": 1, "path": str(music / "a.mp3"), "samplerate": 0, "title": "A"},
            {"id": 2, "path": str(music / "b.mp3"), "samplerate": 44100, "title": "B"},
        ],
        cues=[
            {"id": 1, "track_id": 1, "type": 1, "position": 1000, "hotcue": 0},
            {"id": 2, "track_id": 2, "type": 6, "position": 1000, "hotcue": -1},
        ],
        playlists=[],
        playlist_tracks=[],
        crates=[],
        crate_tracks=[],
    )
    mixxx = read_mixxx_library(db)
    assert mixxx.notes.get("cues dropped: no samplerate in Mixxx") == 1
    assert mixxx.notes.get("cues dropped: Mixxx cue type 6 has no open-dj kind") == 1


def test_hidden_tracks_are_dropped_and_counted(tmp_path: Path) -> None:
    """[if] library.mixxx_deleted is set [then] the track is hidden and counted, [else stop]."""
    music = tmp_path / "music"
    music.mkdir()
    db = _make_db(
        tmp_path / "mixxxdb.sqlite",
        tracks=[{"id": 1, "path": str(music / "a.mp3"), "mixxx_deleted": 1, "title": "H"}],
        cues=[],
        playlists=[],
        playlist_tracks=[],
        crates=[],
        crate_tracks=[],
    )
    read = vendor.read_vendor("mixxx", db)
    assert read.reader_notes.get("track dropped: hidden in Mixxx") == 1
    assert read.library.tracks == ()


def test_a_file_that_is_not_a_mixxx_library_is_refused(tmp_path: Path) -> None:
    """[if] required Mixxx tables are missing [then] ValueError or FileNotFoundError is raised, [else stop]."""
    missing = tmp_path / "bad.sqlite"
    conn = sqlite3.connect(missing)
    conn.execute("CREATE TABLE library (id INTEGER)")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="missing table track_locations"):
        read_mixxx_library(missing)
    with pytest.raises(FileNotFoundError):
        read_mixxx_library(tmp_path / "nope.sqlite")


def test_a_pre_2_3_library_without_color_columns_still_reads(tmp_path: Path) -> None:
    """[if] color columns are absent [then] tracks and cues still read, [else stop]."""
    lib = read_mixxx_library(_make_db(tmp_path / "mixxxdb.sqlite", with_color=False)).library
    assert len(lib.tracks) == 3
    assert all(t.color_rgb is None for t in lib.tracks)


def _wire_ready(lib):
    id_map = {t.track_id: hashlib.sha1(t.track_id.encode()).hexdigest() for t in lib.tracks}

    def remap_ids(track_ids: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(id_map.get(tid, tid) for tid in track_ids)

    def remap_track(t):
        ext = dict(t.extensions)
        ext["content_hash"] = "sha256:" + ("b" * 64)
        return replace(t, track_id=id_map[t.track_id], extensions=ext)

    def remap_playlist(p):
        return replace(
            p,
            track_ids=remap_ids(p.track_ids),
            children=tuple(remap_playlist(c) for c in p.children),
        )

    tracks = tuple(remap_track(t) for t in lib.tracks)
    playlists = tuple(remap_playlist(p) for p in lib.playlists)
    return replace(lib, tracks=tracks, playlists=playlists)


def test_the_read_library_is_a_valid_open_dj_wire_document(tmp_path: Path) -> None:
    """[if] the reader output is remapped to wire ids [then] validate_document accepts it, [else stop]."""
    db = _standard_db(tmp_path)
    raw = read_mixxx_library(db).library
    ready = _wire_ready(raw)
    assert validate_document(library_to_wire_document(ready, source="open-dj-tool")) == []
    again = read_mixxx_library(db).library
    assert serialize_jcs(raw.to_dict()) == serialize_jcs(again.to_dict())


def test_the_cli_imports_a_mixxx_library_as_vendor_mixxx(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """[if] library_import main runs with --write [then] mixxx vendor rows land in state, [else stop]."""
    db = _standard_db(tmp_path)
    for t in read_mixxx_library(db).library.tracks:
        Path(t.file_path).parent.mkdir(parents=True, exist_ok=True)
        Path(t.file_path).write_bytes(b"")
    state_path = tmp_path / "state.db"
    code = vendor.main(["mixxx", "--source", str(db), "--db", str(state_path), "--write"])
    assert code == 0
    capsys.readouterr()
    conn = state_db.open_rw(state_path)
    try:
        n_tracks = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        assert n_tracks == 3
        vendors = {row[0] for row in conn.execute("SELECT vendor FROM track_vendor_ids")}
        assert vendors == {"mixxx"}
        pl_names = {row[0] for row in conn.execute("SELECT name FROM playlists WHERE vendor = 'mixxx'")}
        assert "Set" in pl_names
        vendor_pl = {row[0] for row in conn.execute("SELECT vendor_pl_id FROM playlists WHERE vendor = 'mixxx'")}
        assert "Crates/Techno" in vendor_pl
        key_src = conn.execute("SELECT source FROM track_fields WHERE field_name = 'key' LIMIT 1").fetchone()
        assert key_src and key_src[0] == "open-dj-tool"
        hot_slots: set[str] = set()
        for (sid,) in conn.execute("SELECT stable_id FROM tracks"):
            blob, _ = cue_store.read_row(conn, sid)
            hot_slots.update(c["slot"] for c in blob["cues"] if c["kind"] == "hot_cue" and c.get("slot"))
        assert hot_slots >= {"A", "C"}
    finally:
        conn.close()


def test_ingest_is_fill_only_and_never_overwrites_another_source(tmp_path: Path) -> None:
    """[if] rekordbox already owns a BPM [then] mixxx import keeps it, [else stop]."""
    _STAMP = "2026-11-09T00:00:00+00:00"
    db = _standard_db(tmp_path)
    lib = read_mixxx_library(db).library
    t0 = lib.tracks[0]
    path = t0.file_path
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(b"")
    conn = state_db.open_rw(tmp_path / "state.db")
    writer = StateWriter(conn, bus=FakeEventBus(), actor="test")
    try:
        writer.upsert_track(
            stable_id="seeded-rekordbox",
            stable_id_tier="inferred",
            title=t0.title,
            artists=list(t0.artists),
            album=t0.album,
            isrc=None,
            duration_ms=t0.duration_ms,
            file_path=path,
            content_hash=None,
            audio_hash=None,
        )
        writer.set_field("seeded-rekordbox", "bpm", 99.0, source="rekordbox", modified_at=_STAMP)
        report = vendor.ingest_library(
            writer,
            lib,
            vendor="mixxx",
            source=str(db),
            resolve_path=vendor._decoded_path,
            dry_run=False,
        )
        row = conn.execute(
            "SELECT value_json FROM track_fields WHERE stable_id = ? AND field_name = 'bpm'",
            ("seeded-rekordbox",),
        ).fetchone()
        assert row is not None
        import json

        bpm = json.loads(row[0])
    finally:
        writer.close()
        conn.close()
    assert report.fields_kept.get("bpm") == 1
    assert float(bpm) == 99.0


def test_default_source_prefers_the_sandboxed_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] both Mixxx default paths exist [then] the sandboxed one wins, [else stop]."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    plain = tmp_path / DEFAULT_LIBRARY_CANDIDATES[1]
    plain.parent.mkdir(parents=True, exist_ok=True)
    plain.write_bytes(b"")
    assert vendor.default_source("mixxx") == plain
    sandbox = tmp_path / DEFAULT_LIBRARY_CANDIDATES[0]
    sandbox.parent.mkdir(parents=True, exist_ok=True)
    sandbox.write_bytes(b"x")
    assert vendor.default_source("mixxx") == sandbox
    plain.unlink()
    sandbox.unlink()
    assert vendor.default_source("mixxx") is None
