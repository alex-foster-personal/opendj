"""Phase 17 Plan 03 step 3.5 -- FTS5 fixture test.

Builds a tiny in-memory ``tracks`` + ``tracks_fts`` pair matching the
schemas the launcher expects and asserts a BM25-weighted prefix query
returns the expected row. Also exercises ``apps.launcher.scripts.bootstrap_db``
end-to-end with a fabricated row source so we prove the schema + FTS
population are wired correctly.

[if] launcher FTS5 bootstrap runs [then] prefix search and stable_id hashing match LAUNCH-01, [else stop].
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.requirement("LAUNCH-01")

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_bootstrap_module():
    """Load ``apps/launcher/scripts/bootstrap_db.py`` as a module.

    The launcher scripts folder is not a Python package (no __init__.py) so we
    use importlib.util to grab the file directly.
    """
    path = REPO_ROOT / "apps" / "launcher" / "scripts" / "bootstrap_db.py"
    spec = importlib.util.spec_from_file_location("_launcher_bootstrap", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["_launcher_bootstrap"] = module
    spec.loader.exec_module(module)
    return module


def test_fts5_returns_known_track(tmp_path: Path) -> None:
    """Bare SQLite assert -- prefix match on a seeded `tracks_fts` finds the row."""
    db = tmp_path / "fts.sqlite"
    conn = sqlite3.connect(db)
    try:
        conn.executescript(
            """
            CREATE TABLE tracks(
                stable_id TEXT PRIMARY KEY, path TEXT, title TEXT, artist TEXT,
                album TEXT, genre TEXT, key TEXT, bpm REAL, duration_ms INTEGER,
                isrc TEXT, source TEXT
            );
            CREATE VIRTUAL TABLE tracks_fts USING fts5(
                title, artist, album, genre, key, tags,
                tokenize='unicode61 remove_diacritics 2'
            );
            INSERT INTO tracks(stable_id, path, title, artist)
                VALUES ('s1', '/x.mp3', 'Neon Orchard', 'Mira Valen');
            INSERT INTO tracks_fts(rowid, title, artist, album, genre, key, tags)
                SELECT rowid, title, artist, album, genre, key, '' FROM tracks;
            """
        )
        conn.commit()
        cur = conn.execute(
            """
            SELECT t.stable_id, t.title
            FROM tracks_fts f JOIN tracks t ON t.rowid = f.rowid
            WHERE tracks_fts MATCH 'mira*'
            """
        )
        row = cur.fetchone()
        assert row == ("s1", "Neon Orchard")
    finally:
        conn.close()


def test_bootstrap_db_build_and_query(tmp_path: Path) -> None:
    """End-to-end: bootstrap_db.build_db populates both tables; FTS5 finds inserts."""
    bootstrap = _load_bootstrap_module()
    dst = tmp_path / "bootstrap.sqlite"
    rows = [
        {"path": "/a/Neon Orchard.mp3", "title": "Neon Orchard", "artist": "Mira Valen",
         "album": "Cold Signals", "genre": "Pop", "key": "11A", "bpm": 103,
         "duration_ms": 203_000, "isrc": None, "source": "rekordbox"},
        {"path": "/b/VelvetStatic.mp3", "title": "Velvet Static", "artist": "The Lowlands",
         "album": "Night Shift", "genre": "Synthwave", "key": "11B", "bpm": 171,
         "duration_ms": 200_000, "isrc": None, "source": "rekordbox"},
    ]
    count = bootstrap.build_db(dst, rows)
    assert count == 2, f"expected 2 inserts, got {count}"

    conn = sqlite3.connect(dst)
    try:
        assert conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM tracks_fts").fetchone()[0] == 2

        hits = conn.execute(
            """
            SELECT t.title FROM tracks_fts f
            JOIN tracks t ON t.rowid = f.rowid
            WHERE tracks_fts MATCH 'lowlands*'
            """
        ).fetchall()
        assert hits == [("Velvet Static",)]
    finally:
        conn.close()


def test_bootstrap_db_dedupes_identical_paths(tmp_path: Path) -> None:
    """Repeated rows with the same path collapse to a single stable_id."""
    bootstrap = _load_bootstrap_module()
    dst = tmp_path / "dedup.sqlite"
    rows = [
        {"path": "/dup/song.mp3", "title": "X", "artist": "Y", "album": "",
         "genre": "", "key": None, "bpm": None, "duration_ms": None, "isrc": None,
         "source": "rekordbox"},
        {"path": "/dup/song.mp3", "title": "X", "artist": "Y", "album": "",
         "genre": "", "key": None, "bpm": None, "duration_ms": None, "isrc": None,
         "source": "djay"},
    ]
    count = bootstrap.build_db(dst, rows)
    assert count == 1, "dedupe by stable_id (path-derived) should collapse to one row"


def test_stable_id_is_deterministic() -> None:
    bootstrap = _load_bootstrap_module()
    sid = bootstrap.stable_id_from_path("/music/x.mp3")
    # SHA-1 truncated to 16 hex chars.
    assert len(sid) == 16
    # Hex output must be lowercase so the id comparisons in SQL remain stable
    # regardless of which platform wrote the row.
    assert sid == sid.lower()
    assert all(c in "0123456789abcdef" for c in sid)
    # Same input path produces the same id.
    assert sid == bootstrap.stable_id_from_path("/music/x.mp3")
    # Case-folding behaviour mirrors ``os.path.normcase``: on Windows the path
    # is case-insensitive so the two spellings collapse to the same id, on
    # POSIX (macOS + Linux, which is what we ship on) ``normcase`` is a no-op
    # so the spellings produce distinct ids. The previous form of this test
    # used ``or sid != sid.upper()`` which was trivially true for any hex
    # string and therefore never exercised the normcase branch.
    import sys
    other = bootstrap.stable_id_from_path("/music/X.MP3")
    if sys.platform.startswith("win"):
        assert sid == other, "on Windows case-insensitive paths must collapse"
    else:
        assert sid != other, "on POSIX case-sensitive paths must produce distinct ids"
