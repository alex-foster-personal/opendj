"""RBXAN-8322i: adopt writes Rekordbox analysis into existing Open DJ tracks only.

[if] adopt runs over a Rekordbox library [then] analysis lands only on tracks Open DJ already holds and no new track is created, [else stop].

-Claude
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.rekordbox_analyse import service
from apps.shared.state import db as state_db
from tests.rekordbox_analyse.rb_fixture import FixtureTrack, build_library

pytestmark = pytest.mark.requirement("RBXAN-8322i")
NOW = "2026-11-05T00:00:00+00:00"


def _state(path: Path, rows: list[tuple[str, str, str, str]]) -> Path:
    conn = state_db.open_rw(path)
    with conn:
        for sid, file_path, title, playlist in rows:
            conn.execute(
                "INSERT INTO tracks (stable_id, stable_id_tier, title, file_path, created_at, updated_at) "
                "VALUES (?, 'inferred', ?, ?, ?, ?)",
                (sid, title, file_path, NOW, NOW),
            )
            conn.execute(
                "INSERT OR IGNORE INTO playlists (playlist_id, name, vendor, vendor_pl_id, created_at, updated_at) "
                "VALUES (?, ?, 'odj', ?, ?, ?)",
                (playlist, playlist, playlist, NOW, NOW),
            )
            conn.execute(
                "INSERT INTO playlist_memberships (playlist_id, stable_id, position) "
                "VALUES (?, ?, (SELECT COUNT(*) FROM playlist_memberships WHERE playlist_id = ?))",
                (playlist, sid, playlist),
            )
    conn.close()
    return path


@pytest.fixture
def where(tmp_path: Path) -> service.Locations:
    lib = build_library(
        tmp_path / "rb",
        [
            FixtureTrack("10", "/m/viva.mp3", True, bpm=128.0),
            FixtureTrack("11", "/m/viva-unanalysed.mp3", False, bpm=None),
            FixtureTrack("20", "/m/elsewhere.mp3", True, bpm=140.0),
        ],
        [],
    )
    state = _state(
        tmp_path / "state.db",
        [
            ("s1", "/m/viva.mp3", "My title", "Viva - The cut"),
            ("s2", "/m/viva-unanalysed.mp3", "Two", "Viva - The cut"),
            ("s3", "/m/house.mp3", "House one", "House"),
        ],
    )
    return service.Locations(lib.master_db, state, tmp_path / "bk")


def _rows(state: Path, sql: str) -> list[tuple]:
    with sqlite3.connect(state) as conn:
        return conn.execute(sql).fetchall()


def test_dry_run_matches_analysed_in_scope_only(where: service.Locations) -> None:
    result = service.adopt(where, ("Viva",), dry_run=True)
    assert result["matched"] == 1 and result["bpm"] == 1, (
        "if an unanalysed or out-of-scope Rekordbox track is adopted then broken"
    )


def test_live_adopt_writes_fields_never_tracks(where: service.Locations) -> None:
    before = _rows(where.state_db, "SELECT stable_id, title, file_path FROM tracks ORDER BY stable_id")
    result = service.adopt(where, ("Viva",))
    assert result["adopted"] and result["bpm"] == 1 and result["vendor_links"] == 1
    assert _rows(where.state_db, "SELECT stable_id, title, file_path FROM tracks ORDER BY stable_id") == before, (
        "if adopt inserts tracks or overwrites titles with Rekordbox's then broken (seen in the library-wide ingest)"
    )
    assert _rows(where.state_db, "SELECT stable_id, vendor_id FROM track_vendor_ids WHERE vendor = 'rekordbox'") == [
        ("s1", "10")
    ]
    assert _rows(where.state_db, "SELECT COUNT(*) FROM playlists WHERE vendor = 'rekordbox'") == [(0,)]
