"""iTunes / Apple Music Library XML reader and playlist import (issue #9752).

[if] the Library XML reader or import drifts from rekordbox's iTunes source (by-location match) [then] fail, [else stop].

Regression one-liners:
  - if a Location URI is not decoded (spaces, NFD accents) then no track ever matches
  - if master, built-in or smart playlists are imported then the list fills with library views
  - if playlist items lose iTunes order then sets play out of order
  - if a playlist does not land under the iTunes folder then imports scatter the tree
  - if an unmatched location is dropped silently then the DJ never learns what is missing
  - if a re-run duplicates playlists instead of updating them then every import adds copies
"""

from __future__ import annotations

import sqlite3
import unicodedata
from pathlib import Path

import pytest

from apps.adapters import itunes_xml
from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter

pytestmark = pytest.mark.requirement("DJIMPORT-14534")

FIXTURE = Path(__file__).parent / "itunes_xml" / "Library.xml"
ALPHA = "/fixture/Music/Alpha One.mp3"
CAFE_NFC = unicodedata.normalize("NFC", "/fixture/Music/Café.mp3")
TRACKS = {"t-alpha": ALPHA, "t-cafe": CAFE_NFC}


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    connection = state_db.open_rw(tmp_path / "state.db")
    writer = StateWriter(connection, bus=FakeEventBus(), actor="test-seed")
    for sid, path in TRACKS.items():
        writer.upsert_track(
            stable_id=sid,
            stable_id_tier="inferred",
            title=sid,
            artists=[],
            album=None,
            isrc=None,
            duration_ms=200_000,
            file_path=path,
        )
    return connection


def _import(
    conn: sqlite3.Connection, xml: bytes | None = None, *, dry_run: bool = False
) -> itunes_xml.ItunesImportReport:
    library = itunes_xml.parse_itunes_xml(xml if xml is not None else FIXTURE.read_bytes())
    writer = StateWriter(conn, bus=FakeEventBus(), actor="test")
    return itunes_xml.import_itunes_library(writer, library, source=str(FIXTURE), dry_run=dry_run)


def _playlists(conn: sqlite3.Connection) -> dict[str, tuple[str, str | None, str]]:
    rows = conn.execute("SELECT playlist_id, name, parent_id, kind FROM playlists WHERE deleted_at IS NULL").fetchall()
    return {name: (pid, parent, kind) for pid, name, parent, kind in rows}


def _members(conn: sqlite3.Connection, playlist_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT stable_id FROM playlist_memberships WHERE playlist_id = ? AND deleted_at IS NULL ORDER BY position",
        (playlist_id,),
    ).fetchall()
    return [row[0] for row in rows]


def test_reader_decodes_locations_and_keeps_item_order() -> None:
    """[if] a Location URI is left encoded or Playlist Items lose order [then] fail, [else stop]."""
    library = itunes_xml.read_itunes_xml(FIXTURE)
    assert library.locations["101"] == ALPHA
    assert unicodedata.normalize("NFC", library.locations["102"]) == CAFE_NFC
    assert "104" not in library.locations
    warmup = next(p for p in library.playlists if p.name == "Warmup")
    assert warmup.track_ids == ("102", "101", "103")
    assert warmup.parent_key == "AAAA000000000004"


def test_reader_skips_master_built_in_and_smart_playlists() -> None:
    """[if] the master, a built-in view or a smart playlist is kept [then] fail, [else stop]."""
    library = itunes_xml.read_itunes_xml(FIXTURE)
    assert [p.name for p in library.playlists] == ["Sets", "Warmup", "Peak"]
    assert library.skipped == {"master": 1, "built_in": 1, "smart": 1}


def test_reader_refuses_a_file_that_is_not_a_library_plist() -> None:
    """[if] a non-plist or a plist without Tracks is read as an empty library [then] fail, [else stop]."""
    with pytest.raises(itunes_xml.ItunesXmlError):
        itunes_xml.parse_itunes_xml(b"<html>nope</html>")
    with pytest.raises(itunes_xml.ItunesXmlError):
        itunes_xml.parse_itunes_xml(b'<?xml version="1.0"?><plist version="1.0"><dict></dict></plist>')


def test_import_writes_playlists_under_itunes_folder_matched_by_path(conn: sqlite3.Connection) -> None:
    """[if] playlists miss the iTunes folder, lose order or mis-match paths [then] fail, [else stop]."""
    report = _import(conn)
    tree = _playlists(conn)
    root_id, root_parent, root_kind = tree["iTunes"]
    assert (root_parent, root_kind) == (None, "folder")
    sets_id, sets_parent, sets_kind = tree["Sets"]
    assert (sets_parent, sets_kind) == (root_id, "folder")
    warmup_id, warmup_parent, warmup_kind = tree["Warmup"]
    assert (warmup_parent, warmup_kind) == (sets_id, "playlist")
    peak_id, peak_parent, _ = tree["Peak"]
    assert peak_parent == root_id
    assert _members(conn, warmup_id) == ["t-cafe", "t-alpha"]
    assert _members(conn, peak_id) == ["t-alpha"]
    assert (report.playlists_written, report.folders_written) == (2, 1)
    assert (report.entries_matched, report.entries_unmatched) == (3, 2)


def test_import_reports_unmatched_locations(conn: sqlite3.Connection) -> None:
    """[if] an unmatched location or a location-less item is not reported [then] fail, [else stop]."""
    report = _import(conn)
    assert report.unmatched_locations == ["/fixture/Music/Missing.mp3", "track 104 (no file location)"]
    assert conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == len(TRACKS)


def test_reimport_updates_in_place(conn: sqlite3.Connection) -> None:
    """[if] a second import adds playlists instead of renaming and refilling the same rows [then] fail, [else stop]."""
    _import(conn)
    first = _playlists(conn)
    changed = FIXTURE.read_bytes().replace(b"<string>Peak</string>", b"<string>Peak Time</string>")
    changed = changed.replace(
        b"<dict><key>Track ID</key><integer>102</integer></dict>\n\t\t\t\t<dict><key>Track ID</key><integer>101</integer></dict>",
        b"<dict><key>Track ID</key><integer>101</integer></dict>\n\t\t\t\t<dict><key>Track ID</key><integer>102</integer></dict>",
    )
    _import(conn, changed)
    second = _playlists(conn)
    assert len(second) == len(first) == 4
    assert second["Peak Time"][0] == first["Peak"][0]
    assert _members(conn, second["Warmup"][0]) == ["t-alpha", "t-cafe"]


def test_dry_run_writes_nothing(conn: sqlite3.Connection) -> None:
    """[if] a dry run leaves playlists behind [then] fail, [else stop]."""
    report = _import(conn, dry_run=True)
    assert report.playlists_written == 2
    assert _playlists(conn) == {}
