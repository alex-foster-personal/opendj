"""rekordbox XML exports read into an OpenDjLibrary (DJIMPORT-06).

[if] a DJ_PLAYLISTS export is read [then] its tracks, cues, grid and playlists come back intact, [else stop].
Every track here is invented.
"""
from __future__ import annotations

import re
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from apps.adapters.rekordbox_xml import read_rekordbox_xml
from apps.adapters.rekordbox_xml.reader import location_to_path

pytestmark = pytest.mark.requirement("DJIMPORT-06")

XML = """<?xml version="1.0" encoding="UTF-8"?>
<DJ_PLAYLISTS Version="1.0.0">
  <PRODUCT Name="Lexicon" Version="1.11" Company="Lexicon"/>
  <COLLECTION Entries="3">
    <TRACK TrackID="11" Name="Invented Tune" Artist="Invented Artist" Album="Invented LP"
           TotalTime="242" AverageBpm="128.00" Tonality="Am" Rating="204" PlayCount="7"
           Comments="peak time" Colour="0xFF007F"
           Location="file://localhost/Volumes/Invented%20DJ/Invented%20Tune.mp3">
      <TEMPO Inizio="0.025" Bpm="128.00" Metro="4/4" Battito="1"/>
      <POSITION_MARK Name="Drop" Type="0" Start="32.000" Num="2" Red="40" Green="226" Blue="20"/>
      <POSITION_MARK Name="" Type="0" Start="1.000" Num="-1"/>
      <POSITION_MARK Name="Roll" Type="4" Start="64.000" End="68.000" Num="-1"/>
    </TRACK>
    <TRACK TrackID="12" Name="Invented Two" Location="file://localhost/C:/Music/two.mp3"/>
    <TRACK TrackID="13" Name="Invented Stream" Location="tidal:tracks:1"/>
  </COLLECTION>
  <PLAYLISTS>
    <NODE Type="0" Name="ROOT" Count="2">
      <NODE Type="0" Name="Gigs" Count="1">
        <NODE Name="Invented Friday" Type="1" KeyType="0" Entries="2">
          <TRACK Key="12"/><TRACK Key="11"/>
        </NODE>
      </NODE>
      <NODE Name="By Path" Type="1" KeyType="1" Entries="1">
        <TRACK Key="file://localhost/Volumes/Invented%20DJ/Invented%20Tune.mp3"/>
      </NODE>
    </NODE>
  </PLAYLISTS>
</DJ_PLAYLISTS>
"""


@pytest.fixture
def parsed(tmp_path: Path):
    path = tmp_path / "rekordbox.xml"
    path.write_text(XML)
    return read_rekordbox_xml(path)


def test_tracks_fields_and_product(parsed) -> None:
    """[if] a TRACK carries tags, rating and color [then] they map to open-dj fields, [else stop]."""
    assert parsed.product == "Lexicon"
    assert parsed.tracks_without_location == 1  # the streaming entry
    one = parsed.library.tracks[0]
    assert one.file_path == "/Volumes/Invented DJ/Invented Tune.mp3"
    assert (one.title, one.artists, one.album) == ("Invented Tune", ("Invented Artist",), "Invented LP")
    assert (one.bpm, one.key_camelot, one.rating, one.play_count) == (128.0, "Am", 4, 7)
    assert one.duration_ms == 242000
    assert one.color_rgb == 0xFF007F
    assert one.extensions["x_comment"] == "peak time"
    assert [(b.position_ms, b.bpm) for b in one.beats] == [(25, 128.0)]


def test_position_marks_keep_pads_memory_and_loops(parsed) -> None:
    """[if] a mark has Num 2, Num -1 or Type 4 [then] it is pad 2, a memory cue or a loop, [else stop]."""
    cues = parsed.library.tracks[0].cues
    assert [(c.type, c.index, c.position_ms, c.length_ms) for c in cues] == [
        ("hot", 2, 32000, None),
        ("memory", 1, 1000, None),
        ("loop", 2, 64000, 4000),
    ]
    assert cues[0].color_rgb == (40 << 16) | (226 << 8) | 20


def test_playlists_resolve_by_track_id_and_by_location(parsed) -> None:
    """[if] playlists key tracks by TrackID or Location, inside folders [then] both resolve, [else stop]."""
    gigs, by_path = parsed.library.playlists
    assert gigs.name == "Gigs" and gigs.track_ids == ()
    assert gigs.children[0].name == "Invented Friday"
    assert gigs.children[0].track_ids == ("rbxml_12", "rbxml_11")
    assert by_path.track_ids == ("rbxml_11",)


def test_locations_decode_for_macos_and_windows() -> None:
    """[if] a Location is a file URL [then] it decodes to a local path, and anything else is None, [else stop]."""
    assert location_to_path("file://localhost/Volumes/Invented%20DJ/a%20b/c%C3%A9.mp3") == "/Volumes/Invented DJ/a b/c\u00e9.mp3"
    assert location_to_path("file://localhost/C:/Music/x.mp3") == "C:/Music/x.mp3"
    assert location_to_path("file:///Volumes/Invented/a.mp3") == "/Volumes/Invented/a.mp3"
    assert location_to_path("file://LOCALHOST/Volumes/Invented/a.mp3") == "/Volumes/Invented/a.mp3"
    # A share on another host keeps its host as a UNC path, never a local one.
    assert location_to_path("file://nas/share/a%20b.mp3") == "//nas/share/a b.mp3"
    assert location_to_path("tidal:tracks:1") is None
    assert location_to_path("") is None


def test_a_file_that_is_not_dj_playlists_is_refused(tmp_path: Path) -> None:
    """[if] the XML root is not DJ_PLAYLISTS [then] reading raises, [else stop]."""
    path = tmp_path / "other.xml"
    path.write_text("<NML/>")
    with pytest.raises(ValueError, match="not a rekordbox XML library"):
        read_rekordbox_xml(path)


def _one_track(tmp_path: Path, attrs: str, marks: str = "") -> Path:
    path = tmp_path / "one.xml"
    path.write_text(
        '<DJ_PLAYLISTS Version="1.0.0"><COLLECTION Entries="1">'
        f'<TRACK TrackID="7" Name="Invented" Location="file://localhost/Volumes/Invented/a.mp3" {attrs}>{marks}</TRACK>'
        "</COLLECTION></DJ_PLAYLISTS>"
    )
    return path


@pytest.mark.parametrize(("attr", "field", "expected"), [('PlayCount="7,0"', "play_count", 7), ('Rating="153,0"', "rating", 3)])
def test_a_comma_decimal_whole_number_is_read_as_that_whole_number(tmp_path: Path, attr: str, field: str, expected: int) -> None:
    """[if] a whole-number field is written with a comma decimal, such as 7,0 [then] it reads as that whole number, not an abort, [else stop]."""
    (track,) = read_rekordbox_xml(_one_track(tmp_path, attr)).library.tracks
    assert getattr(track, field) == expected


@pytest.mark.parametrize(
    ("attrs", "marks", "named"),
    [
        ('AverageBpm="fast"', "", "AverageBpm='fast'"),
        ('Colour="0xZZ"', "", "Colour='0xZZ'"),
        ("", '<POSITION_MARK Type="0" Start="1,5,6" Num="0"/>', "Start='1,5,6'"),
        ('Rating="153.9"', "", "Rating='153.9' is not a whole number"),
        ('PlayCount="-3"', "", "PlayCount='-3' is negative"),
        ('PlayCount="1e30"', "", "PlayCount='1e30' is too large to store"),
        ('PlayCount="1e1000000"', "", "PlayCount='1e1000000' is too large to store"),
        ('PlayCount="9223372036854775808"', "", "PlayCount='9223372036854775808' is too large to store"),
        ('PlayCount="7.0000000000000001"', "", "PlayCount='7.0000000000000001' is not a whole number"),
        ('TotalTime="1e100"', "", "TotalTime='1e100' is too large to store"),
        ('TotalTime="1e308"', "", "TotalTime='1e308' is too large to store"),
        ("", '<POSITION_MARK Type="0" Start="1" Num="2.9"/>', "Num='2.9' is not a whole number"),
        ("", '<POSITION_MARK Type="0" Start="1" Num="1" Red="40.5" Green="0" Blue="0"/>', "Red='40.5' is not a whole number"),
        ("", '<POSITION_MARK Type="0" Start="1" Num="1" Red="40"/>', "Red, Green and Blue must be given together"),
        ("", '<POSITION_MARK Type="0" Start="1" Num="1" Green="x"/>', "Green='x' is not a number"),
        ("", '<POSITION_MARK Type="0" Start="1" Num="1" Red="256" Green="0" Blue="0"/>', "Red='256' is outside 0-255"),
        ("", '<POSITION_MARK Type="0" Start="1" Num="8"/>', "Num='8' is neither -1"),
        ("", '<POSITION_MARK Type="0" Start="1" Num="-2"/>', "Num='-2' is neither -1"),
        ('Rating="999"', "", "Rating='999' is not a rekordbox rating"),
        ('Rating="100"', "", "Rating='100' is not a rekordbox rating"),
        ('Colour="-1"', "", "Colour='-1' is not a 24-bit RGB color"),
        ('Colour="0x1FF007F"', "", "Colour='0x1FF007F' is not a 24-bit RGB color"),
        ('TotalTime="-5"', "", "TotalTime='-5' is negative"),
        ("", '<POSITION_MARK Type="0" Start="-1" Num="0"/>', "Start='-1' is negative"),
        ("", '<POSITION_MARK Start="1" Num="0"/>', "POSITION_MARK Type=None is not a rekordbox mark type"),
        ("", '<POSITION_MARK Type="9" Start="1" Num="0"/>', "POSITION_MARK Type='9' is not a rekordbox mark type"),
        ("", '<POSITION_MARK Type="0" Num="0"/>', "POSITION_MARK has no Start"),
        ("", '<POSITION_MARK Type="0" Start="" Num="0"/>', "POSITION_MARK has no Start"),
        ("", '<POSITION_MARK Type="4" Start="64" Num="-1"/>', "loop End=None is not after Start='64'"),
        ("", '<POSITION_MARK Type="4" Start="64" End="64" Num="-1"/>', "loop End='64' is not after Start='64'"),
        ("", '<POSITION_MARK Type="4" Start="64" End="60" Num="-1"/>', "loop End='60' is not after Start='64'"),
        ("", '<POSITION_MARK Type="4" Start="64" End="-1" Num="-1"/>', "End='-1' is negative"),
        ("", '<TEMPO Inizio="0.025"/>', "TEMPO needs both Inizio and Bpm"),
        ("", '<TEMPO Bpm="128"/>', "TEMPO needs both Inizio and Bpm"),
        ("", '<TEMPO Inizio="-0.5" Bpm="128"/>', "Inizio='-0.5' is negative"),
        ("", '<TEMPO Inizio="0" Bpm="0"/>', "TEMPO Bpm='0' is not positive"),
    ],
)
def test_a_malformed_number_stops_the_import_naming_track_and_attribute(
    tmp_path: Path, attrs: str, marks: str, named: str
) -> None:
    """[if] a numeric attribute does not parse [then] reading fails naming the track and attribute, [else stop]."""
    with pytest.raises(ValueError, match=rf"TrackID='7': {named}"):
        read_rekordbox_xml(_one_track(tmp_path, attrs, marks))


def test_blank_numbers_are_missing_not_errors(tmp_path: Path) -> None:
    """[if] numeric attributes are blank [then] they read as missing and the import goes on, [else stop]."""
    (track,) = read_rekordbox_xml(
        _one_track(tmp_path, 'AverageBpm="" Rating="" Colour="" PlayCount=""')
    ).library.tracks
    assert (track.bpm, track.rating, track.color_rgb, track.play_count) == (None, None, None, 0)


@pytest.mark.parametrize(
    ("seconds", "ms"),
    [
        ("9223372036854775", 9223372036854775000),  # 2**63 - 1 ms bound: converting to float first rounded this past the limit
        ("1.2345", 1234),
        ("1.2355", 1236),  # ties round to even, as the float path did
        ("0,5", 500),
    ],
)
def test_a_time_near_the_store_limit_is_scaled_exactly(tmp_path: Path, seconds: str, ms: int) -> None:
    """[if] TotalTime is a large but storable number of seconds [then] it scales to milliseconds exactly, [else stop]."""
    (track,) = read_rekordbox_xml(_one_track(tmp_path, f'TotalTime="{seconds}"')).library.tracks
    assert track.duration_ms == ms


def test_a_zero_with_a_large_exponent_reads_as_zero(tmp_path: Path) -> None:
    """[if] a whole number is zero with a large exponent [then] it reads as 0, not as too large, [else stop]."""
    (track,) = read_rekordbox_xml(_one_track(tmp_path, 'PlayCount="0e1000000"')).library.tracks
    assert track.play_count == 0


def test_whole_numbers_written_as_decimals_still_read(tmp_path: Path) -> None:
    """[if] a whole number is written as 2.0 [then] it reads as 2, so the integral check does not overshoot, [else stop]."""
    (track,) = read_rekordbox_xml(
        _one_track(tmp_path, 'Rating="153.0" PlayCount="7.0"', '<POSITION_MARK Type="0" Start="1" Num="2.0"/>')
    ).library.tracks
    assert (track.rating, track.play_count) == (3, 7)
    assert [(c.type, c.index) for c in track.cues] == [("hot", 2)]


@pytest.mark.parametrize("count", [2**53 + 1, 2**63 - 1])
def test_a_whole_number_past_float_precision_keeps_every_digit(tmp_path: Path, count: int) -> None:
    """[if] a whole number is past 2**53 but within what the store holds [then] it reads exactly, never rounded or rejected, [else stop]."""
    (track,) = read_rekordbox_xml(_one_track(tmp_path, f'PlayCount="{count}"')).library.tracks
    assert track.play_count == count


def test_a_hot_cue_with_no_pad_number_is_a_memory_cue(tmp_path: Path) -> None:
    """[if] a Type 0 mark has no Num [then] it is a memory cue at its ordinal, never a pad, [else stop]."""
    (track,) = read_rekordbox_xml(
        _one_track(
            tmp_path,
            "",
            '<POSITION_MARK Type="0" Start="1" Num="0"/>'
            '<POSITION_MARK Type="0" Start="2"/>'
            '<POSITION_MARK Type="0" Start="3" Num=""/>',
        )
    ).library.tracks
    assert [(c.type, c.index, c.position_ms) for c in track.cues] == [
        ("hot", 0, 1000),
        ("memory", 1, 2000),
        ("memory", 2, 3000),
    ]


def test_an_export_with_no_collection_is_refused(tmp_path: Path) -> None:
    """[if] DJ_PLAYLISTS has no COLLECTION [then] reading raises instead of importing nothing, [else stop]."""
    path = tmp_path / "truncated.xml"
    path.write_text('<DJ_PLAYLISTS Version="1.0.0"><PRODUCT Name="rekordbox"/></DJ_PLAYLISTS>')
    with pytest.raises(ValueError, match="has no <COLLECTION>"):
        read_rekordbox_xml(path)


def test_an_empty_collection_is_an_empty_library(tmp_path: Path) -> None:
    """[if] COLLECTION is present but empty [then] the library is empty and no error is raised, [else stop]."""
    path = tmp_path / "empty.xml"
    path.write_text('<DJ_PLAYLISTS Version="1.0.0"><COLLECTION Entries="0"/></DJ_PLAYLISTS>')
    assert read_rekordbox_xml(path).library.tracks == ()


def test_every_valid_rating_pad_and_color_edge_still_reads(tmp_path: Path) -> None:
    """[if] values sit on the edges of their ranges [then] they read, so the new checks do not overshoot, [else stop]."""
    (track,) = read_rekordbox_xml(
        _one_track(
            tmp_path,
            'Rating="255" Colour="0xFFFFFF"',
            '<POSITION_MARK Type="0" Start="1" Num="7" Red="255" Green="0" Blue="255"/>'
            '<POSITION_MARK Type="0" Start="2" Num="-1" Red="0" Green="0" Blue="0"/>',
        )
    ).library.tracks
    assert (track.rating, track.color_rgb) == (5, 0xFFFFFF)
    assert [(c.type, c.index, c.color_rgb) for c in track.cues] == [("hot", 7, 0xFF00FF), ("memory", 1, 0)]
    for raw, stars in (("0", 0), ("51", 1), ("204", 4)):
        (one,) = read_rekordbox_xml(_one_track(tmp_path, f'Rating="{raw}"')).library.tracks
        assert one.rating == stars


def test_valid_marks_and_grid_at_their_edges_still_read(tmp_path: Path) -> None:
    """[if] marks sit on valid edges [then] they read, so the new checks do not overshoot, [else stop]."""
    (track,) = read_rekordbox_xml(
        _one_track(
            tmp_path,
            'TotalTime="0"',
            '<TEMPO Inizio="0" Bpm="0.5"/>'
            '<POSITION_MARK Type="0" Start="0" Num="0"/>'
            '<POSITION_MARK Type=" 3 " Start="1"/>'
            '<POSITION_MARK Type="0" Start="2" End="1" Num="1"/>'
            '<POSITION_MARK Type="4" Start="3" End="3.001" Num="-1"/>',
        )
    ).library.tracks
    assert track.duration_ms == 0
    assert [(b.position_ms, b.bpm) for b in track.beats] == [(0, 0.5)]
    assert [(c.type, c.position_ms, c.length_ms) for c in track.cues] == [
        ("hot", 0, None),
        ("load", 1000, None),
        ("hot", 2000, None),
        ("loop", 3000, 1),
    ]


def test_zero_bpm_is_unanalyzed_and_a_negative_bpm_stops_the_import(tmp_path: Path) -> None:
    """[if] AverageBpm is 0.00 [then] it reads as missing (rekordbox's unanalyzed value), and a negative one raises, [else stop]."""
    (track,) = read_rekordbox_xml(_one_track(tmp_path, 'AverageBpm="0.00"')).library.tracks
    assert track.bpm is None
    (track,) = read_rekordbox_xml(_one_track(tmp_path, 'AverageBpm="0.5"')).library.tracks
    assert track.bpm == 0.5
    with pytest.raises(ValueError, match=r"TrackID='7': AverageBpm='-1' is negative"):
        read_rekordbox_xml(_one_track(tmp_path, 'AverageBpm="-1"'))


def _playlists(tmp_path: Path, nodes: str) -> Path:
    path = tmp_path / "lists.xml"
    root_count = len(ET.fromstring(f"<R>{nodes}</R>"))
    path.write_text(
        '<DJ_PLAYLISTS Version="1.0.0"><COLLECTION Entries="0"/>'
        f'<PLAYLISTS><NODE Type="0" Name="ROOT" Count="{root_count}">{nodes}</NODE></PLAYLISTS></DJ_PLAYLISTS>'
    )
    return path


@pytest.mark.parametrize(
    ("node", "named"),
    [
        ('<NODE Name="A" KeyType="0"/>', "Type=None is neither 0"),
        ('<NODE Name="A" Type="2" KeyType="0"/>', "Type='2' is neither 0"),
        ('<NODE Name="A" Type="1" KeyType=""/>', "KeyType='' is neither 0"),
        ('<NODE Name="A" Type="1" KeyType="2"/>', "KeyType='2' is neither 0"),
    ],
)
def test_an_unknown_node_or_key_type_stops_the_import(tmp_path: Path, node: str, named: str) -> None:
    """[if] a NODE Type is not 0/1 or a playlist KeyType is not 0/1 [then] reading raises naming the node, [else stop]."""
    with pytest.raises(ValueError, match=rf"NODE Name='A': {named}"):
        read_rekordbox_xml(_playlists(tmp_path, node))


def test_folder_and_both_key_types_still_read(tmp_path: Path) -> None:
    """[if] Type and KeyType carry their documented values [then] the tree reads, [else stop]."""
    path = tmp_path / "types.xml"
    path.write_text(
        '<DJ_PLAYLISTS Version="1.0.0"><COLLECTION Entries="2">'
        '<TRACK TrackID="3" Location="file://localhost/Volumes/Invented/a.mp3"/>'
        '<TRACK TrackID="4" Location="file://localhost/Volumes/Invented/b.mp3"/>'
        "</COLLECTION>"
        '<PLAYLISTS><NODE Type="0" Name="ROOT" Count="3">'
        '<NODE Type="0" Name="F" Count="1"><NODE Type="1" Name="ById" KeyType="0" Entries="1"><TRACK Key="3"/></NODE></NODE>'
        '<NODE Type=" 1 " Name="ByPath" KeyType="1" Entries="0"/>'
        '<NODE Type="1" Name="NoKeyType" Entries="1"><TRACK Key="4"/></NODE>'
        "</NODE></PLAYLISTS></DJ_PLAYLISTS>"
    )
    lists = read_rekordbox_xml(path).library.playlists
    assert [(p.name, [c.name for c in p.children]) for p in lists] == [("F", ["ById"]), ("ByPath", []), ("NoKeyType", [])]
    assert lists[2].track_ids == ("rbxml_4",)
    assert lists[0].children[0].track_ids == ("rbxml_3",)


def test_a_track_with_no_track_id_stops_the_import(tmp_path: Path) -> None:
    """[if] a TRACK with a Location has no TrackID [then] reading raises naming its Location, [else stop]."""
    path = tmp_path / "no_id.xml"
    path.write_text(
        '<DJ_PLAYLISTS Version="1.0.0"><COLLECTION Entries="1">'
        '<TRACK Name="Invented" Location="file://localhost/Volumes/Invented/a.mp3"/>'
        "</COLLECTION></DJ_PLAYLISTS>"
    )
    with pytest.raises(ValueError, match=re.escape("Location='file://localhost/Volumes/Invented/a.mp3' has no TrackID")):
        read_rekordbox_xml(path)


@pytest.mark.parametrize("entry", ['<TRACK/>', '<TRACK Key=" "/>'])
def test_a_playlist_entry_with_no_key_stops_the_import(tmp_path: Path, entry: str) -> None:
    """[if] a playlist TRACK has no Key or a blank one [then] reading raises naming the playlist, [else stop]."""
    with pytest.raises(ValueError, match=r"NODE Name='Gigs': a TRACK has no Key"):
        read_rekordbox_xml(_playlists(tmp_path, f'<NODE Type="1" Name="Gigs" KeyType="0" Entries="1">{entry}</NODE>'))


@pytest.mark.parametrize(
    ("root", "named"),
    [
        ('<NODE Name="ROOT">', "Type=None is not 0"),
        ('<NODE Type="1" Name="ROOT" KeyType="0">', "Type='1' is not 0"),
        ('<NODE Type="2" Name="ROOT">', "Type='2' is not 0"),
    ],
)
def test_a_root_node_that_is_not_a_folder_stops_the_import(tmp_path: Path, root: str, named: str) -> None:
    """[if] the top PLAYLISTS NODE is not Type 0 [then] reading raises naming the root, [else stop]."""
    path = tmp_path / "lists.xml"
    path.write_text(
        '<DJ_PLAYLISTS Version="1.0.0"><COLLECTION Entries="0"/>'
        f'<PLAYLISTS>{root}<NODE Type="1" Name="A" KeyType="0"/></NODE></PLAYLISTS></DJ_PLAYLISTS>'
    )
    with pytest.raises(ValueError, match=rf"PLAYLISTS: root NODE Name='ROOT': {named}"):
        read_rekordbox_xml(path)


def test_a_root_folder_written_with_spaces_still_reads(tmp_path: Path) -> None:
    """[if] the root NODE is Type " 0 " [then] its playlists read, [else stop]."""
    path = tmp_path / "lists.xml"
    path.write_text(
        '<DJ_PLAYLISTS Version="1.0.0"><COLLECTION Entries="0"/>'
        '<PLAYLISTS><NODE Type=" 0 " Name="ROOT" Count="1"><NODE Type="1" Name="A" KeyType="0" Entries="0"/></NODE></PLAYLISTS></DJ_PLAYLISTS>'
    )
    assert [p.name for p in read_rekordbox_xml(path).library.playlists] == ["A"]


@pytest.mark.parametrize(
    ("nodes", "named"),
    [
        (
            '<NODE Type="0" Name="F" Count="0"><NODE Type="1" Name="A" KeyType="0" Entries="0"/></NODE>',
            "folder NODE Name='F' Count='0' but 1 children are present",
        ),
        (
            '<NODE Type="0" Name="F"><NODE Type="1" Name="A" KeyType="0" Entries="0"/></NODE>',
            "folder NODE Name='F' has no Count",
        ),
    ],
)
def test_a_folder_that_declares_a_different_count_stops_the_import(tmp_path: Path, nodes: str, named: str) -> None:
    """[if] a folder's Count is missing or differs from its child nodes [then] reading raises naming the folder, [else stop]."""
    with pytest.raises(ValueError, match=re.escape(named)):
        read_rekordbox_xml(_playlists(tmp_path, nodes))


def test_a_root_that_declares_a_different_count_stops_the_import(tmp_path: Path) -> None:
    """[if] the top ROOT Count differs from its child nodes [then] reading raises naming the root, [else stop]."""
    path = tmp_path / "lists.xml"
    path.write_text(
        '<DJ_PLAYLISTS Version="1.0.0"><COLLECTION Entries="0"/>'
        '<PLAYLISTS><NODE Type="0" Name="ROOT" Count="2"><NODE Type="1" Name="A" KeyType="0" Entries="0"/></NODE></PLAYLISTS>'
        "</DJ_PLAYLISTS>"
    )
    with pytest.raises(ValueError, match=r"PLAYLISTS: root NODE Name='ROOT' Count='2' but 1 children are present"):
        read_rekordbox_xml(path)


def test_a_loop_on_a_pad_keeps_its_pad_as_a_hot_loop(tmp_path: Path) -> None:
    """[if] a Type 4 mark has Num 0-7 [then] it stays on that pad with the loop's length, and an unpadded loop stays a loop, [else stop]."""
    from apps.adapters.library_import import VendorIngestReport, store_cues

    cues = read_rekordbox_xml(
        _one_track(
            tmp_path,
            "",
            '<POSITION_MARK Type="4" Start="10" End="12" Num="0"/>'
            '<POSITION_MARK Type="4" Start="20" End="24" Num="7"/>'
            '<POSITION_MARK Type="4" Start="30" End="31" Num="-1"/>',
        )
    ).library.tracks[0].cues
    assert [(c.type, c.index, c.position_ms, c.length_ms) for c in cues] == [
        ("hot", 0, 10000, 2000),
        ("hot", 7, 20000, 4000),
        ("loop", 2, 30000, 1000),
    ]
    stored = store_cues(cues, VendorIngestReport(vendor="rekordbox_xml", source="x"))
    assert [(c["kind"], c["slot"], c["in_ms"], c["out_ms"]) for c in stored] == [
        ("hot_cue", "A", 10000, 12000),
        ("hot_cue", "H", 20000, 24000),
        ("loop", None, 30000, 31000),
    ]


def test_a_comma_decimal_separator_reads_on_float_fields(tmp_path: Path) -> None:
    """[if] a float field uses a comma decimal separator, as the format allows [then] it reads as that value, [else stop]."""
    path = _one_track(
        tmp_path,
        'AverageBpm="128,5"',
        '<POSITION_MARK Type="0" Start="1,5" Num="0"/>',
    )
    track = read_rekordbox_xml(path).library.tracks[0]
    assert track.bpm == 128.5
    assert track.cues[0].position_ms == 1500


def test_a_collection_that_declares_more_tracks_than_it_holds_stops_the_import(tmp_path: Path) -> None:
    """[if] COLLECTION Entries is not the number of TRACK children [then] reading raises naming both, [else stop]."""
    path = tmp_path / "short.xml"
    path.write_text(
        '<DJ_PLAYLISTS Version="1.0.0"><COLLECTION Entries="2">'
        '<TRACK TrackID="7" Name="Invented" Location="file://localhost/Volumes/Invented/a.mp3"/>'
        "</COLLECTION></DJ_PLAYLISTS>"
    )
    with pytest.raises(ValueError, match=r"COLLECTION Entries='2' but 1 children are present"):
        read_rekordbox_xml(path)


@pytest.mark.parametrize(
    ("attrs", "named"),
    [
        ('Entries="2"', "Entries='2' but 1 children are present"),
        ('Entries="0"', "Entries='0' but 1 children are present"),
        ("", "has no Entries"),
    ],
)
def test_a_playlist_that_declares_a_different_count_stops_the_import(tmp_path: Path, attrs: str, named: str) -> None:
    """[if] a playlist's Entries is missing or is not its TRACK count [then] reading raises naming the playlist, [else stop]."""
    node = f'<NODE Type="1" Name="Gigs" KeyType="0" {attrs}><TRACK Key="3"/></NODE>'
    with pytest.raises(ValueError, match=rf"playlist NODE Name='Gigs' {named}"):
        read_rekordbox_xml(_playlists(tmp_path, node))


def test_two_marks_on_one_pad_stop_the_import_naming_the_track(tmp_path: Path) -> None:
    """[if] two POSITION_MARKs take the same hot-cue pad [then] reading raises naming the track and pad, [else stop]."""
    path = _one_track(
        tmp_path,
        "",
        '<POSITION_MARK Type="0" Start="1" Num="2"/><POSITION_MARK Type="0" Start="9" Num="2"/>',
    )
    with pytest.raises(ValueError, match=r"TrackID='7': two POSITION_MARKs take hot-cue pad 2"):
        read_rekordbox_xml(path)


def test_two_tracks_with_one_track_id_stop_the_import_naming_both_locations(tmp_path: Path) -> None:
    """[if] two non-skipped TRACKs share a TrackID [then] reading raises naming both locations, [else stop]."""
    path = tmp_path / "dup.xml"
    path.write_text(
        '<DJ_PLAYLISTS Version="1.0.0"><COLLECTION Entries="2">'
        '<TRACK TrackID="5" Location="file://localhost/Volumes/Invented/a.mp3"/>'
        '<TRACK TrackID="5" Location="file://localhost/Volumes/Invented/b.mp3"/>'
        "</COLLECTION></DJ_PLAYLISTS>"
    )
    with pytest.raises(ValueError, match=r"TrackID='5' is on two tracks, 'file://localhost/Volumes/Invented/a.mp3' and 'file://localhost/Volumes/Invented/b.mp3'"):
        read_rekordbox_xml(path)


@pytest.mark.parametrize("order", ["streaming_first", "local_first"])
def test_a_streaming_track_and_a_local_track_sharing_a_track_id_stop_the_import(tmp_path: Path, order: str) -> None:
    """[if] a skipped streaming TRACK and a local TRACK share a TrackID, in either order [then] reading raises naming both, [else stop]."""
    streaming = '<TRACK TrackID="5" Name="Streaming"/>'
    local = '<TRACK TrackID="5" Location="file://localhost/Volumes/Invented/a.mp3"/>'
    children = streaming + local if order == "streaming_first" else local + streaming
    path = tmp_path / "shared_skipped.xml"
    path.write_text(
        f'<DJ_PLAYLISTS Version="1.0.0"><COLLECTION Entries="2">{children}</COLLECTION></DJ_PLAYLISTS>'
    )
    with pytest.raises(ValueError, match=r"TrackID='5' is on two tracks"):
        read_rekordbox_xml(path)


def test_two_collection_sections_stop_the_import(tmp_path: Path) -> None:
    """[if] DJ_PLAYLISTS holds two COLLECTION sections [then] reading raises instead of dropping the second, [else stop]."""
    path = tmp_path / "two_collections.xml"
    path.write_text(
        '<DJ_PLAYLISTS Version="1.0.0"><COLLECTION Entries="0"/><COLLECTION Entries="0"/></DJ_PLAYLISTS>'
    )
    with pytest.raises(ValueError, match="has 2 <COLLECTION> sections, expected one"):
        read_rekordbox_xml(path)


def test_two_playlists_sections_stop_the_import(tmp_path: Path) -> None:
    """[if] DJ_PLAYLISTS holds two PLAYLISTS sections [then] reading raises instead of dropping the second's playlists, [else stop]."""
    path = tmp_path / "two_playlists.xml"
    path.write_text(
        '<DJ_PLAYLISTS Version="1.0.0"><COLLECTION Entries="0"/>'
        "<PLAYLISTS><NODE Type=\"0\" Name=\"ROOT\" Count=\"0\"/></PLAYLISTS>"
        "<PLAYLISTS><NODE Type=\"0\" Name=\"ROOT\" Count=\"0\"/></PLAYLISTS>"
        "</DJ_PLAYLISTS>"
    )
    with pytest.raises(ValueError, match="has 2 <PLAYLISTS> sections, expected at most one"):
        read_rekordbox_xml(path)


def test_two_sibling_playlists_with_one_name_stop_the_import(tmp_path: Path) -> None:
    """[if] two playlists under one folder share a name [then] reading raises instead of replacing one's members, [else stop]."""
    path = tmp_path / "sibling_names.xml"
    path.write_text(
        '<DJ_PLAYLISTS Version="1.0.0"><COLLECTION Entries="0"/>'
        '<PLAYLISTS><NODE Type="0" Name="ROOT" Count="2">'
        '<NODE Name="Warmup" Type="1" KeyType="0" Entries="0"/>'
        '<NODE Name="Warmup" Type="1" KeyType="0" Entries="0"/>'
        "</NODE></PLAYLISTS></DJ_PLAYLISTS>"
    )
    with pytest.raises(ValueError, match="appears twice under one folder"):
        read_rekordbox_xml(path)


def test_a_streaming_track_with_no_track_id_stops_the_import(tmp_path: Path) -> None:
    """[if] a streaming TRACK has no TrackID [then] reading raises before the location skip, [else stop]."""
    path = tmp_path / "stream_no_id.xml"
    path.write_text(
        '<DJ_PLAYLISTS Version="1.0.0"><COLLECTION Entries="1">'
        '<TRACK Name="Streamed" Location="tidal:track:1"/>'
        "</COLLECTION></DJ_PLAYLISTS>"
    )
    with pytest.raises(ValueError, match=re.escape("Location='tidal:track:1' has no TrackID")):
        read_rekordbox_xml(path)


@pytest.mark.parametrize(
    ("body", "named"),
    [
        ("", "holds 0 root NODEs"),
        ('<NODE Type="0" Name="A" Count="0"/><NODE Type="0" Name="B" Count="0"/>', "holds 2 root NODEs"),
    ],
)
def test_a_playlists_block_without_exactly_one_root_stops_the_import(tmp_path: Path, body: str, named: str) -> None:
    """[if] PLAYLISTS holds zero or several root NODEs [then] reading raises naming the count, [else stop]."""
    path = tmp_path / "roots.xml"
    path.write_text(
        '<DJ_PLAYLISTS Version="1.0.0"><COLLECTION Entries="0"/>'
        f"<PLAYLISTS>{body}</PLAYLISTS></DJ_PLAYLISTS>"
    )
    with pytest.raises(ValueError, match=rf"PLAYLISTS {named}, expected exactly one"):
        read_rekordbox_xml(path)


def test_a_location_keyed_entry_that_matches_no_track_stops_the_import(tmp_path: Path) -> None:
    """[if] a KeyType=1 playlist entry names a Location no TRACK has [then] reading raises, [else stop]."""
    with pytest.raises(ValueError, match=r"NODE Name='By Path': Location='file://localhost/Volumes/Gone/x.mp3' matches no COLLECTION TRACK"):
        read_rekordbox_xml(
            _playlists(
                tmp_path,
                '<NODE Type="1" Name="By Path" KeyType="1" Entries="1">'
                '<TRACK Key="file://localhost/Volumes/Gone/x.mp3"/></NODE>',
            )
        )


def test_a_track_id_entry_for_a_skipped_streaming_track_keeps_its_id_as_unresolved(tmp_path: Path) -> None:
    """[if] a TrackID-keyed entry names a streaming TRACK that was skipped [then] the id stays on the playlist
    for the ingest to count as unresolved, [else stop]."""
    path = tmp_path / "skipped_member.xml"
    path.write_text(
        '<DJ_PLAYLISTS Version="1.0.0"><COLLECTION Entries="2">'
        '<TRACK TrackID="1" Location="file://localhost/Volumes/Invented/a.mp3"/>'
        '<TRACK TrackID="2" Location="tidal:track:9"/>'
        "</COLLECTION>"
        '<PLAYLISTS><NODE Type="0" Name="ROOT" Count="1">'
        '<NODE Type="1" Name="Mixed" KeyType="0" Entries="2">'
        '<TRACK Key="1"/><TRACK Key="2"/>'
        "</NODE></NODE></PLAYLISTS></DJ_PLAYLISTS>"
    )
    result = read_rekordbox_xml(path)
    assert result.tracks_without_location == 1
    assert result.library.playlists[0].track_ids == ("rbxml_1", "rbxml_2")


def test_a_location_entry_for_a_skipped_streaming_track_keeps_its_id_as_unresolved(tmp_path: Path) -> None:
    """[if] a Location-keyed entry names a streaming TRACK that was skipped [then] the id stays on the playlist
    for the ingest to count as unresolved, [else stop]."""
    path = tmp_path / "skipped_location.xml"
    path.write_text(
        '<DJ_PLAYLISTS Version="1.0.0"><COLLECTION Entries="2">'
        '<TRACK TrackID="1" Location="file://localhost/Volumes/Invented/a.mp3"/>'
        '<TRACK TrackID="2" Location="tidal:track:9"/>'
        "</COLLECTION>"
        '<PLAYLISTS><NODE Type="0" Name="ROOT" Count="1">'
        '<NODE Type="1" Name="Mixed" KeyType="1" Entries="2">'
        '<TRACK Key="file://localhost/Volumes/Invented/a.mp3"/>'
        '<TRACK Key="tidal:track:9"/>'
        "</NODE></NODE></PLAYLISTS></DJ_PLAYLISTS>"
    )
    result = read_rekordbox_xml(path)
    assert result.library.playlists[0].track_ids == ("rbxml_1", "rbxml_2")


def test_a_track_id_entry_that_matches_no_track_stops_the_import(tmp_path: Path) -> None:
    """[if] a TrackID-keyed entry names no COLLECTION TRACK [then] reading raises naming the id, [else stop]."""
    with pytest.raises(ValueError, match=r"NODE Name='Gigs': TrackID='99' matches no COLLECTION TRACK"):
        read_rekordbox_xml(_playlists(tmp_path, '<NODE Type="1" Name="Gigs" KeyType="0" Entries="1"><TRACK Key="99"/></NODE>'))


def test_two_tracks_on_one_location_stop_the_import_naming_both_ids(tmp_path: Path) -> None:
    """[if] two non-skipped TRACKs share a Location [then] reading raises naming both TrackIDs, [else stop]."""
    path = tmp_path / "dup_location.xml"
    path.write_text(
        '<DJ_PLAYLISTS Version="1.0.0"><COLLECTION Entries="2">'
        '<TRACK TrackID="5" Location="file://localhost/Volumes/Invented/a.mp3"/>'
        '<TRACK TrackID="6" Location="file://localhost/Volumes/Invented/a.mp3"/>'
        "</COLLECTION></DJ_PLAYLISTS>"
    )
    with pytest.raises(ValueError, match=r"Location='file://localhost/Volumes/Invented/a.mp3' is on two tracks, TrackID='5' and TrackID='6'"):
        read_rekordbox_xml(path)


def test_two_skipped_streaming_tracks_on_one_location_stop_the_import(tmp_path: Path) -> None:
    """[if] two skipped streaming TRACKs share a Location [then] reading raises naming both TrackIDs, [else stop]."""
    path = tmp_path / "dup_streaming.xml"
    path.write_text(
        '<DJ_PLAYLISTS Version="1.0.0"><COLLECTION Entries="2">'
        '<TRACK TrackID="8" Location="tidal:track:9"/>'
        '<TRACK TrackID="9" Location="tidal:track:9"/>'
        "</COLLECTION></DJ_PLAYLISTS>"
    )
    with pytest.raises(ValueError, match=r"Location='tidal:track:9' is on two tracks, TrackID='8' and TrackID='9'"):
        read_rekordbox_xml(path)


def test_several_tracks_with_no_location_are_all_skipped_not_refused(tmp_path: Path) -> None:
    """[if] several TRACKs have no Location [then] each is skipped and counted, none is refused as a duplicate, [else stop]."""
    path = tmp_path / "blank.xml"
    path.write_text(
        '<DJ_PLAYLISTS Version="1.0.0"><COLLECTION Entries="2">'
        '<TRACK TrackID="8"/>'
        '<TRACK TrackID="9"/>'
        "</COLLECTION></DJ_PLAYLISTS>"
    )
    assert read_rekordbox_xml(path).tracks_without_location == 2
