"""VirtualDJ database.xml reader tests (DJIMPORT-10056).

[if] a VirtualDJ database.xml and MyLists tree is read [then] tracks, cues, playlists and counters match the fixture, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.adapters.virtualdj import read_virtualdj_library, virtualdj_path_resolver
from apps.open_dj import BeatGridPoint

pytestmark = [
    pytest.mark.requirement("DJIMPORT-10056"),
    pytest.mark.requirement("DJIMPORT-10631"),
]

_FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "virtualdj" / "VirtualDJ"


def _by_title(read):
  by = {t.title: t for t in read.library.tracks}
  return read, by


def test_fixture_three_songs_read_core_fields() -> None:
    """[if] the fixture database is read [then] three songs carry title, bpm, key, duration, play count and rating, [else stop]."""
    read, by = _by_title(read_virtualdj_library(_FIXTURE_ROOT, windows=False))
    one, two = by["Invented Tune One"], by["Invented Tune Two"]
    three = next(t for t in read.library.tracks if "Three" in t.file_path)
    assert one.artists == ("Invented Artist",)
    assert one.bpm == 128.0
    assert one.key_camelot == "8A"
    assert one.duration_ms == 242500
    assert one.play_count == 7
    assert one.rating == 4
    assert two.bpm == 124.0
    assert two.key_camelot == "11A"
    assert two.duration_ms == 200000
    assert two.play_count == 0
    assert two.rating == 0
    assert three.bpm == 120.0
    assert three.key_camelot == "2A"
    assert three.duration_ms == 180250
    assert three.play_count == 3
    assert three.rating == 5
    assert read.bpm_as_period == 2
    assert read.bpm_as_bpm == 1
    assert one.extensions.get("x_genre") == "House"
    assert one.extensions.get("x_year") == 2019
    assert one.extensions.get("x_bitrate") == 320
    assert one.extensions.get("x_comment") == "Invented comment"
    assert one.extensions.get("x_date_added") == "2023-11-14"


def test_fixture_cues_and_beatgrid() -> None:
    """[if] Song One holds Pois [then] hot, memory, loop and beatgrid read correctly, [else stop]."""
    read, by = _by_title(read_virtualdj_library(_FIXTURE_ROOT, windows=False))
    one = by["Invented Tune One"]
    hots = {(c.index, c.position_ms, c.name) for c in one.cues if c.type == "hot"}
    assert (0, 32000, "Drop") in hots
    assert (7, 96000, "Outro") in hots
    memory = [c for c in one.cues if c.type == "memory"]
    assert len(memory) == 1 and memory[0].position_ms == 120000
    loops = [c for c in one.cues if c.type == "loop"]
    assert len(loops) == 1 and loops[0].position_ms == 64000 and loops[0].length_ms == 7500
    assert one.beats == (BeatGridPoint(25, 128.0),)
    assert read.pois_skipped == {"automix": 1}


def test_fixture_playlists_and_unresolved() -> None:
    """[if] MyLists holds vdjfolder and m3u playlists [then] membership order and unresolved counts hold, [else stop]."""
    read = read_virtualdj_library(_FIXTURE_ROOT, windows=False)
    warmup = next(p for p in read.library.playlists if p.name == "Warmup")
    ids_two = next(t.track_id for t in read.library.tracks if t.title == "Invented Tune Two")
    ids_one = next(t.track_id for t in read.library.tracks if t.title == "Invented Tune One")
    assert warmup.track_ids[0] == ids_two
    assert warmup.track_ids[1] == ids_one
    assert len(warmup.track_ids) == 3
    assert read.playlist_entries_unresolved == 1
    sets = next(p for p in read.library.playlists if p.name == "Sets")
    peak = sets.children[0]
    assert peak.name == "Peak"
    three = next(t.track_id for t in read.library.tracks if "Three" in t.file_path)
    assert peak.track_ids == (three, ids_one)
    assert read.filter_folders_skipped == 1


def test_windows_path_verbatim() -> None:
    """[if] a Windows path is in the database [then] it stays verbatim and counts unresolved off Windows, [else stop]."""
    read = read_virtualdj_library(_FIXTURE_ROOT, windows=False)
    three = next(t for t in read.library.tracks if "Three" in t.file_path)
    assert three.file_path == "D:\\Music\\Invented Tune Three.mp3"
    assert read.windows_paths_unresolved == 1
    read_win = read_virtualdj_library(_FIXTURE_ROOT, windows=True)
    assert read_win.windows_paths_unresolved == 0
    resolver = virtualdj_path_resolver(windows=False)
    assert resolver(three.file_path) == three.file_path


def test_bad_root_raises(tmp_path: Path) -> None:
    """[if] database.xml has the wrong root [then] the read raises, [else stop]."""
    db = tmp_path / "database.xml"
    db.write_text('<?xml version="1.0"?><Wrong></Wrong>', encoding="utf-8")
    with pytest.raises(ValueError, match="not a VirtualDJ database"):
        read_virtualdj_library(tmp_path, windows=False)


def test_duplicate_filepath_raises(tmp_path: Path) -> None:
    """[if] two Songs share a FilePath [then] the read raises, [else stop]."""
    db = tmp_path / "database.xml"
    db.write_text(
        '<?xml version="1.0"?><VirtualDJ_Database>'
        '<Song FilePath="/x/a.mp3"><Infos SongLength="1"/></Song>'
        '<Song FilePath="/x/a.mp3"><Infos SongLength="1"/></Song>'
        "</VirtualDJ_Database>",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate FilePath"):
        read_virtualdj_library(tmp_path, windows=False)


def test_streaming_song_dropped(tmp_path: Path) -> None:
    """[if] FilePath is a streaming URL [then] the song is dropped and counted, [else stop]."""
    db = tmp_path / "database.xml"
    db.write_text(
        '<?xml version="1.0"?><VirtualDJ_Database>'
        '<Song FilePath="netsearch://abc"><Infos SongLength="1"/></Song>'
        '<Song FilePath="/local/a.mp3"><Tags Title="A"/><Infos SongLength="10"/></Song>'
        "</VirtualDJ_Database>",
        encoding="utf-8",
    )
    read = read_virtualdj_library(tmp_path, windows=False)
    assert read.songs_streaming == 1
    assert len(read.library.tracks) == 1


def test_unknown_key_counts(tmp_path: Path) -> None:
    """[if] key text is unknown [then] keys_unrecognized increments, [else stop]."""
    db = tmp_path / "database.xml"
    db.write_text(
        '<?xml version="1.0"?><VirtualDJ_Database>'
        '<Song FilePath="/a.mp3"><Tags Key="Xyz"/><Infos SongLength="10"/></Song>'
        "</VirtualDJ_Database>",
        encoding="utf-8",
    )
    read = read_virtualdj_library(tmp_path, windows=False)
    assert read.keys_unrecognized == 1
    assert read.library.tracks[0].key_camelot is None


def test_bpm_out_of_range_dropped(tmp_path: Path) -> None:
    """[if] BPM is outside 10..999 [then] bpm_dropped increments, [else stop]."""
    db = tmp_path / "database.xml"
    db.write_text(
        '<?xml version="1.0"?><VirtualDJ_Database>'
        '<Song FilePath="/a.mp3"><Tags Bpm="5000"/><Infos SongLength="10"/></Song>'
        "</VirtualDJ_Database>",
        encoding="utf-8",
    )
    read = read_virtualdj_library(tmp_path, windows=False)
    assert read.bpm_dropped == 1
    assert read.library.tracks[0].bpm is None


def test_two_cues_same_pad_raises(tmp_path: Path) -> None:
    """[if] two cue Pois share a Num pad [then] the read raises, [else stop]."""
    db = tmp_path / "database.xml"
    db.write_text(
        '<?xml version="1.0"?><VirtualDJ_Database>'
        '<Song FilePath="/a.mp3"><Infos SongLength="10"/>'
        '<Poi Pos="1" Type="cue" Num="1"/><Poi Pos="2" Type="cue" Num="1"/>'
        "</Song></VirtualDJ_Database>",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="two cue Pois"):
        read_virtualdj_library(tmp_path, windows=False)


def test_loop_without_size_raises(tmp_path: Path) -> None:
    """[if] a loop Poi has no Size [then] the read raises, [else stop]."""
    db = tmp_path / "database.xml"
    db.write_text(
        '<?xml version="1.0"?><VirtualDJ_Database>'
        '<Song FilePath="/a.mp3"><Infos SongLength="10"/>'
        '<Poi Pos="1" Type="loop"/>'
        "</Song></VirtualDJ_Database>",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="missing Size"):
        read_virtualdj_library(tmp_path, windows=False)


def test_malformed_stars_raises(tmp_path: Path) -> None:
    """[if] Stars is not an integer 0..5 [then] the read raises naming FilePath, [else stop]."""
    db = tmp_path / "database.xml"
    db.write_text(
        '<?xml version="1.0"?><VirtualDJ_Database>'
        '<Song FilePath="/broken/a.mp3"><Tags Stars="six"/><Infos SongLength="10"/></Song>'
        "</VirtualDJ_Database>",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=r"/broken/a\.mp3"):
        read_virtualdj_library(tmp_path, windows=False)


def test_database_xml_path_finds_mylists(tmp_path: Path) -> None:
    """[if] --source points at database.xml itself [then] sibling MyLists is read, [else stop]."""
    root = tmp_path / "vdj"
    root.mkdir()
    (root / "MyLists").mkdir()
    (root / "database.xml").write_text(
        '<?xml version="1.0"?><VirtualDJ_Database>'
        '<Song FilePath="/a.mp3"><Tags Title="Only"/><Infos SongLength="10"/></Song>'
        "</VirtualDJ_Database>",
        encoding="utf-8",
    )
    (root / "MyLists" / "T.vdjfolder").write_text(
        '<?xml version="1.0"?><VirtualFolder><song path="/a.mp3"/></VirtualFolder>',
        encoding="utf-8",
    )
    read = read_virtualdj_library(root / "database.xml", windows=False)
    assert any(p.name == "T" for p in read.library.playlists)


def test_song_without_songlength_imports_with_no_duration(tmp_path: Path) -> None:
    """[if] Infos has no SongLength [then] the track imports with duration_ms None, [else stop]."""
    root = tmp_path / "vdj"
    (root / "MyLists").mkdir(parents=True)
    (root / "database.xml").write_text(
        '<?xml version="1.0"?><VirtualDJ_Database>'
        '<Song FilePath="/minimal.mp3" FileSize="1234">'
        '<Tags Title="NoDuration" Author="Artist"/>'
        "</Song></VirtualDJ_Database>",
        encoding="utf-8",
    )
    track = read_virtualdj_library(root).library.tracks[0]
    assert track.duration_ms is None and track.title == "NoDuration"


def test_loop_num_minus_one_uses_memory_index(tmp_path: Path) -> None:
    """[if] a loop has Num=-1 [then] the library imports with a loop cue, [else stop]."""
    (tmp_path / "database.xml").write_text(
        '<?xml version="1.0"?><VirtualDJ_Database>'
        '<Song FilePath="/a.mp3"><Infos SongLength="120"/>'
        '<Poi Pos="32" Type="loop" Num="-1" Size="4.000000" Name="Unnumbered"/>'
        "</Song></VirtualDJ_Database>",
        encoding="utf-8",
    )
    cues = read_virtualdj_library(tmp_path).library.tracks[0].cues
    assert len(cues) == 1 and cues[0].type == "loop" and cues[0].length_ms == 4000


def test_automix_poi_without_pos_is_skipped(tmp_path: Path) -> None:
    """[if] an automix Poi omits Pos [then] import completes and the poi is skipped, [else stop]."""
    (tmp_path / "database.xml").write_text(
        '<?xml version="1.0"?><VirtualDJ_Database>'
        '<Song FilePath="/a.mp3"><Infos SongLength="60"/>'
        '<Poi Type="automix" Point="realStart"/>'
        '<Poi Type="automix" Point="fadeStart"/>'
        "</Song></VirtualDJ_Database>",
        encoding="utf-8",
    )
    read = read_virtualdj_library(tmp_path)
    assert len(read.library.tracks) == 1
    assert read.pois_skipped.get("automix") == 2
