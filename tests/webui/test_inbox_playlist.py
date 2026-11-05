"""Derived Inbox playlist (LIBM-D1, ADR-0019)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.shared.state import db as state_db
from apps.webui.server.backend import InMemoryBackend, Playlist, Track
from apps.webui.server.inbox_playlist import INBOX_PLAYLIST_ID
from apps.webui.server.sqlite_backend import SqliteBackend

ISO = "2026-04-17T10:00:00.000000Z"


def _iso_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _seed_min_db(db_path: Path, *, extra_track: str | None = None) -> None:
    conn = state_db.open_rw(db_path)
    try:
        conn.execute(
            "INSERT INTO tracks(stable_id, stable_id_tier, title, artists_json, "
            "  album, isrc, duration_ms, file_path, content_hash, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "sid-u",
                "inferred",
                "Unfiled",
                json.dumps(["U"]),
                "U",
                None,
                100_000,
                "/music/u.mp3",
                None,
                ISO,
                ISO,
            ),
        )
        if extra_track is not None:
            conn.execute(
                "INSERT INTO tracks(stable_id, stable_id_tier, title, artists_json, "
                "  album, isrc, duration_ms, file_path, content_hash, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    extra_track,
                    "inferred",
                    "Filed",
                    json.dumps(["F"]),
                    "F",
                    None,
                    100_000,
                    "/music/f.mp3",
                    None,
                    ISO,
                    ISO,
                ),
            )
        conn.execute(
            "INSERT INTO playlists(playlist_id, name, vendor, vendor_pl_id, "
            "  created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("pl-live", "Live", "rekordbox", "RB-1", ISO, ISO),
        )
        if extra_track is not None:
            conn.execute(
                "INSERT INTO playlist_memberships(playlist_id, stable_id, position) VALUES (?, ?, ?)",
                ("pl-live", extra_track, 0),
            )
    finally:
        conn.close()


@pytest.mark.requirement("LIBM-30")
def test_inmemory_unfiled_in_inbox() -> None:
    """[if] a track has no playlist membership [then] it appears in Inbox, [else stop]."""
    backend = InMemoryBackend()
    backend.seed_track(
        Track(
            stable_id="t1",
            title="One",
            bpm=120.0,
            created_at=ISO,
            updated_at=ISO,
        )
    )
    inbox = backend.get_playlist(INBOX_PLAYLIST_ID)
    assert inbox.items == ["t1"]
    backend.seed_playlist(
        Playlist(
            playlist_id="pl-1",
            name="Crate",
            vendor="rekordbox",
            items=["t1"],
            created_at=ISO,
            updated_at=ISO,
        )
    )
    assert backend.get_playlist(INBOX_PLAYLIST_ID).items == []


@pytest.mark.requirement("LIBM-31")
def test_sqlite_unfiled_then_filed(tmp_path: Path) -> None:
    """[if] a track is filed into a live playlist [then] it leaves Inbox, [else stop]."""
    db_path = tmp_path / "state.db"
    _seed_min_db(db_path, extra_track="sid-f")
    backend = SqliteBackend(db_path)
    assert backend.get_playlist(INBOX_PLAYLIST_ID).items == ["sid-u"]
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO playlist_memberships(playlist_id, stable_id, position) VALUES (?, ?, ?)",
        ("pl-live", "sid-u", 1),
    )
    conn.commit()
    conn.close()
    assert backend.get_playlist(INBOX_PLAYLIST_ID).items == []


def test_deleted_playlist_membership_does_not_file(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    _seed_min_db(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO playlist_memberships(playlist_id, stable_id, position) VALUES (?, ?, ?)",
        ("pl-live", "sid-u", 0),
    )
    conn.execute(
        "UPDATE playlists SET deleted_at = ? WHERE playlist_id = ?",
        (_iso_now(), "pl-live"),
    )
    conn.commit()
    conn.close()
    backend = SqliteBackend(db_path)
    assert backend.get_playlist(INBOX_PLAYLIST_ID).items == ["sid-u"]


def test_tombstoned_membership_does_not_file(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    _seed_min_db(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO playlist_memberships(playlist_id, stable_id, position, deleted_at) VALUES (?, ?, ?, ?)",
        ("pl-live", "sid-u", 0, _iso_now()),
    )
    conn.commit()
    conn.close()
    backend = SqliteBackend(db_path)
    assert backend.get_playlist(INBOX_PLAYLIST_ID).items == ["sid-u"]


def test_tombstoned_track_not_in_inbox(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    _seed_min_db(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "UPDATE tracks SET deleted_at = ? WHERE stable_id = ?",
        (_iso_now(), "sid-u"),
    )
    conn.commit()
    conn.close()
    backend = SqliteBackend(db_path)
    assert backend.get_playlist(INBOX_PLAYLIST_ID).items == []


def test_stats_excludes_inbox(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    _seed_min_db(db_path)
    backend = SqliteBackend(db_path)
    assert backend.stats()["playlists"] == 1


def test_inbox_page_slices(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    conn = state_db.open_rw(db_path)
    try:
        for i in range(3):
            sid = f"sid-{i}"
            conn.execute(
                "INSERT INTO tracks(stable_id, stable_id_tier, title, artists_json, "
                "  album, isrc, duration_ms, file_path, content_hash, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sid,
                    "inferred",
                    sid,
                    json.dumps([]),
                    sid,
                    None,
                    100_000,
                    f"/music/{sid}.mp3",
                    None,
                    ISO,
                    ISO,
                ),
            )
        conn.execute(
            "INSERT INTO playlists(playlist_id, name, vendor, vendor_pl_id, "
            "  created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("pl-live", "Live", "rekordbox", "RB-1", ISO, ISO),
        )
    finally:
        conn.close()
    backend = SqliteBackend(db_path)
    page = backend.get_playlist_page(INBOX_PLAYLIST_ID, limit=2, offset=1)
    assert page.total == 3
    assert page.playlist.items == ["sid-1", "sid-2"]
