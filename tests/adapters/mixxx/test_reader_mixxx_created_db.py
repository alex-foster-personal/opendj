"""Mixxx reader against a database Mixxx itself created (DJIMPORT-10059).

AGENTS.md requires one real-path test per database-format boundary. The fixture
``tests/fixtures/mixxx/mixxx-2.5.4-scanned.sqlite`` was written by Mixxx 2.5.4
(schema 39), including one scanned track; see that folder's README for provenance.

[if] a Mixxx-created database is read [then] its scanned track, cues, playlist and crate import, [else stop].
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from apps.adapters.mixxx import read_mixxx_library

pytestmark = pytest.mark.requirement("DJIMPORT-10059")

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "mixxx" / "mixxx-2.5.4-scanned.sqlite"
#: Mixxx 2.x stores cue positions as interleaved stereo sample offsets: 1 s at 44.1 kHz.
ONE_SECOND = 2 * 44100


def _mixxx_db_with_rows(tmp_path: Path) -> Path:
    db = tmp_path / "mixxxdb.sqlite"
    shutil.copyfile(FIXTURE, db)
    conn = sqlite3.connect(db)
    try:
        (track_id,) = conn.execute("SELECT id FROM library").fetchone()
        conn.executemany(
            "INSERT INTO cues (track_id, type, position, length, hotcue, label) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (track_id, 1, ONE_SECOND, 0, 0, "Hot"),
                (track_id, 2, 0, 0, -1, ""),
                (track_id, 4, 0, ONE_SECOND, -1, "Loop"),
            ],
        )
        # Mixxx already wrote its own Auto DJ, history placeholder and set-log playlists.
        playlist_id = conn.execute("INSERT INTO Playlists (name, position, hidden) VALUES ('Set', 4, 0)").lastrowid
        conn.execute("INSERT INTO PlaylistTracks (playlist_id, track_id, position) VALUES (?, ?, 1)", (playlist_id, track_id))
        crate_id = conn.execute("INSERT INTO crates (name) VALUES ('Techno')").lastrowid
        conn.execute("INSERT INTO crate_tracks (crate_id, track_id) VALUES (?, ?)", (crate_id, track_id))
        conn.commit()
    finally:
        conn.close()
    return db


def test_committed_fixture_was_written_by_mixxx_schema_39() -> None:
    """[if] the fixture is opened [then] Mixxx's schema version 39 and its scanned track are there, [else stop]."""
    conn = sqlite3.connect(f"{FIXTURE.as_uri()}?mode=ro", uri=True)
    try:
        version = conn.execute("SELECT value FROM settings WHERE name = 'mixxx.schema.version'").fetchone()
        rows = conn.execute(
            "SELECT l.title, tl.location FROM library l JOIN track_locations tl ON tl.id = l.location"
        ).fetchall()
    finally:
        conn.close()
    assert version == ("39",)
    assert rows == [("tone", "/tmp/mixxx-fixture-music/tone.wav")]


def test_mixxx_created_database_reads_track_cues_playlist_and_crate(tmp_path: Path) -> None:
    """[if] the Mixxx-created database is read [then] the scanned track, cues, playlist and crate import, [else stop]."""
    mixxx = read_mixxx_library(_mixxx_db_with_rows(tmp_path))
    lib = mixxx.library
    assert [(t.title, t.duration_ms, t.file_path) for t in lib.tracks] == [
        ("tone", 2000, "/tmp/mixxx-fixture-music/tone.wav")
    ]
    track = lib.tracks[0]
    assert sorted((c.type, c.position_ms) for c in track.cues) == [("hot", 1000), ("load", 0), ("loop", 0)]
    assert next(c for c in track.cues if c.type == "loop").length_ms == 1000
    assert [p.name for p in lib.playlists] == ["Set", "Crates"]
    assert lib.playlists[0].track_ids == (track.track_id,)
    crates = next(p for p in lib.playlists if p.name == "Crates")
    assert [(c.name, c.track_ids) for c in crates.children] == [("Techno", (track.track_id,))]
    assert (mixxx.tracks_fs_deleted, mixxx.tracks_hidden) == (0, 0)
