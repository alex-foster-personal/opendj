from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from apps.shared.state.schema import apply_migrations

# djmdGenre rows: (ID, Name)
GENRES = {
    "g-techno": "Peak Time Techno",
    "g-house": "Jackin House",
    "g-unmatched": "Spoken Word Poetry",
}


def _make_state_db(
    path: Path,
    *,
    tracks: list[dict],
    memberships: list[dict],
    playlists: list[dict],
    vendor_ids: dict[str, str],
    track_fields: list[dict] | None = None,
) -> None:
    conn = sqlite3.connect(str(path))
    try:
        apply_migrations(conn)
        for t in tracks:
            conn.execute(
                "INSERT INTO tracks (stable_id, stable_id_tier, title, artists_json, "
                "file_path, created_at, updated_at) VALUES (?, 'inferred', ?, ?, ?, "
                "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
                (
                    t["stable_id"],
                    t["title"],
                    json.dumps(t.get("artists", [])),
                    t.get("file_path"),
                ),
            )
        for stable_id, vendor_id in vendor_ids.items():
            conn.execute(
                "INSERT INTO track_vendor_ids (stable_id, vendor, vendor_id) "
                "VALUES (?, 'rekordbox', ?)",
                (stable_id, vendor_id),
            )
        for p in playlists:
            conn.execute(
                "INSERT INTO playlists (playlist_id, name, vendor, vendor_pl_id, "
                "created_at, updated_at) VALUES (?, ?, 'rekordbox', ?, "
                "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
                (p["playlist_id"], p["name"], p["playlist_id"]),
            )
        for i, m in enumerate(memberships):
            conn.execute(
                "INSERT INTO playlist_memberships (playlist_id, stable_id, position) "
                "VALUES (?, ?, ?)",
                (m["playlist_id"], m["stable_id"], i),
            )
        for field in track_fields or []:
            conn.execute(
                "INSERT INTO track_fields (stable_id, field_name, value_json, source, "
                "confidence, modified_at, deleted_at) "
                "VALUES (?, ?, ?, ?, 0.7, '2026-01-01T00:00:00Z', ?)",
                (
                    field["stable_id"],
                    field["field_name"],
                    field["value_json"],
                    field.get("source", "inferred"),
                    field.get("deleted_at"),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _make_master_db(path: Path, *, content: list[dict]) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE djmdGenre (ID VARCHAR(255) PRIMARY KEY, Name VARCHAR(255), "
            "rb_local_deleted TINYINT(1) DEFAULT 0)"
        )
        conn.execute(
            "CREATE TABLE djmdContent (ID VARCHAR(255) PRIMARY KEY, GenreID VARCHAR(255), "
            "FolderPath VARCHAR(255), AnalysisDataPath VARCHAR(255), Commnt VARCHAR(255), "
            "DJPlayCount INTEGER, rb_local_deleted TINYINT(1) DEFAULT 0)"
        )
        for genre_id, name in GENRES.items():
            conn.execute("INSERT INTO djmdGenre (ID, Name) VALUES (?, ?)", (genre_id, name))
        for c in content:
            conn.execute(
                "INSERT INTO djmdContent (ID, GenreID, DJPlayCount) VALUES (?, ?, ?)",
                (c["vendor_id"], c.get("genre_id"), c.get("play_count", 0)),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def wheel_dbs(tmp_path: Path) -> tuple[Path, Path]:
    """A small, deterministic library: 2 techno, 1 house, 1 unmatched-genre,
    1 with no rekordbox mapping at all (unclassified for a different reason)."""
    state_db = tmp_path / "state.db"
    master_db = tmp_path / "master.plain.db"

    tracks = [
        {"stable_id": "t-techno-1", "title": "Techno One", "artists": ["Artist A"]},
        {"stable_id": "t-techno-2", "title": "Techno Two", "artists": ["Artist B"]},
        {"stable_id": "t-house-1", "title": "House One", "artists": ["Artist C"]},
        {"stable_id": "t-unmatched-1", "title": "Spoken One", "artists": ["Artist D"]},
        {"stable_id": "t-no-rb-1", "title": "Local Only", "artists": ["Artist E"]},
    ]
    vendor_ids = {
        "t-techno-1": "v-1",
        "t-techno-2": "v-2",
        "t-house-1": "v-3",
        "t-unmatched-1": "v-4",
        # t-no-rb-1 deliberately has no rekordbox mapping.
    }
    playlists = [{"playlist_id": "pl-1", "name": "Warmup"}]
    memberships = [
        {"playlist_id": "pl-1", "stable_id": "t-techno-1"},
        {"playlist_id": "pl-1", "stable_id": "t-house-1"},
    ]
    _make_state_db(
        state_db, tracks=tracks, memberships=memberships, playlists=playlists,
        vendor_ids=vendor_ids,
    )
    _make_master_db(
        master_db,
        content=[
            {"vendor_id": "v-1", "genre_id": "g-techno", "play_count": 40},
            {"vendor_id": "v-2", "genre_id": "g-techno", "play_count": 10},
            {"vendor_id": "v-3", "genre_id": "g-house", "play_count": 5},
            {"vendor_id": "v-4", "genre_id": "g-unmatched", "play_count": 1},
        ],
    )
    return state_db, master_db


@pytest.fixture
def state_only_wheel_db(tmp_path: Path) -> Path:
    """One unmapped folder-imported track with a House GENRE tag; no master.plain.db."""
    state_db = tmp_path / "state.db"
    tracks = [
        {
            "stable_id": "t-local-house",
            "title": "Folder House",
            "artists": ["Local Artist"],
            "file_path": "/music/folder-house.mp3",
        },
    ]
    _make_state_db(
        state_db,
        tracks=tracks,
        memberships=[],
        playlists=[],
        vendor_ids={},
        track_fields=[
            {
                "stable_id": "t-local-house",
                "field_name": "genre",
                "value_json": '"House"',
            },
        ],
    )
    return state_db
