"""Pathless/stale FolderPath with a present local file is not streaming.

[if] a track with resolvable local audio is listed [then] it is never is_streaming, [else stop].
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.shared.state import db as state_db
from apps.shared.state.writer import StateWriter
from apps.webui.server.app import create_app
from apps.webui.server.backend import Track
from apps.webui.server.rb_vendor_pkg import track_rows
from apps.webui.server.sqlite_backend import SqliteBackend

pytestmark = [pytest.mark.requirement("CAT-05"), pytest.mark.rb_parity]

PATHLESS_PRESENT = "1" * 40
STALE_LOCAL_PRESENT = "2" * 40
STALE_URI_PRESENT = "3" * 40
TRUE_STREAMING = "4" * 40
ORDINARY_LOCAL = "5" * 40

VENDOR_PATHLESS = "207000001"
VENDOR_STALE_LOCAL = "207000002"
VENDOR_STALE_URI = "207000003"
VENDOR_TRUE_STREAMING = "207000004"
VENDOR_ORDINARY = "207000005"

PLAYLIST_ID = "pl-streaming-fix"

PLAYLIST_PLAYABLE_SIDS = (PATHLESS_PRESENT, STALE_LOCAL_PRESENT, STALE_URI_PRESENT)
PLAYABLE_LOCAL_SIDS = (*PLAYLIST_PLAYABLE_SIDS, ORDINARY_LOCAL)


def _mp3(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"ID3" + bytes(1024))
    return path


def _make_master_plain_db(path: Path, audio_dir: Path) -> None:
    ordinary_path = str(_mp3(audio_dir / "ordinary.mp3"))
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE djmdContent (ID VARCHAR(255) PRIMARY KEY, "
            "FolderPath VARCHAR(255), ImagePath VARCHAR(255), "
            "AnalysisDataPath VARCHAR(255), Length INTEGER, "
            "Commnt VARCHAR(255), GenreID VARCHAR(255), "
            "DJPlayCount INTEGER DEFAULT 0, "
            "rb_local_deleted TINYINT(1) DEFAULT 0)"
        )
        conn.execute(
            "CREATE TABLE djmdGenre (ID VARCHAR(255) PRIMARY KEY, "
            "Name VARCHAR(255), rb_local_deleted TINYINT(1) DEFAULT 0)"
        )
        conn.execute(
            "CREATE TABLE djmdCue (ID VARCHAR(255) PRIMARY KEY, "
            "ContentID VARCHAR(255), rb_local_deleted TINYINT(1) DEFAULT 0)"
        )
        rows = [
            (VENDOR_PATHLESS, "", PATHLESS_PRESENT),
            (VENDOR_STALE_LOCAL, "/gone/stale.mp3", STALE_LOCAL_PRESENT),
            (VENDOR_STALE_URI, "spotify:track:olt-local", STALE_URI_PRESENT),
            (VENDOR_TRUE_STREAMING, "tidal:track:abc", TRUE_STREAMING),
            (VENDOR_ORDINARY, ordinary_path, ORDINARY_LOCAL),
        ]
        for vendor_id, folder_path, _sid in rows:
            conn.execute(
                "INSERT INTO djmdContent (ID, FolderPath, Length, rb_local_deleted) "
                "VALUES (?, ?, 300, 0)",
                (vendor_id, folder_path),
            )
        conn.commit()
    finally:
        conn.close()


def _make_state_db(path: Path, audio_dir: Path) -> None:
    ordinary_path = str(_mp3(audio_dir / "ordinary.mp3"))
    location_path = str(_mp3(audio_dir / "location.mp3"))
    conn = state_db.open_rw(path)
    try:
        writer = StateWriter(conn, actor="unit-test")
        track_specs = [
            (PATHLESS_PRESENT, None, VENDOR_PATHLESS),
            (STALE_LOCAL_PRESENT, None, VENDOR_STALE_LOCAL),
            (STALE_URI_PRESENT, None, VENDOR_STALE_URI),
            (TRUE_STREAMING, None, VENDOR_TRUE_STREAMING),
            (ORDINARY_LOCAL, ordinary_path, VENDOR_ORDINARY),
        ]
        for sid, file_path, vendor_id in track_specs:
            writer.upsert_track(
                stable_id=sid,
                stable_id_tier="inferred",
                title=f"track {sid[0]}",
                artists=["Artist"],
                album=None,
                isrc=None,
                duration_ms=180_000,
                file_path=file_path,
            )
            conn.execute(
                "INSERT INTO track_vendor_ids (stable_id, vendor, vendor_id) "
                "VALUES (?, 'rekordbox', ?)",
                (sid, vendor_id),
            )
        for sid in (PATHLESS_PRESENT, STALE_LOCAL_PRESENT, STALE_URI_PRESENT):
            writer.upsert_track_location(
                stable_id=sid,
                kind="local",
                file_path=location_path,
            )
        conn.execute(
            "INSERT INTO playlists (playlist_id, name, vendor, vendor_pl_id, "
            "created_at, updated_at) VALUES (?, 'Streaming fix', 'rekordbox', "
            "'rb-pl-streaming', '2026-01-01', '2026-01-01')",
            (PLAYLIST_ID,),
        )
        playlist_members = (
            PATHLESS_PRESENT,
            STALE_LOCAL_PRESENT,
            STALE_URI_PRESENT,
            TRUE_STREAMING,
        )
        for position, sid in enumerate(playlist_members):
            conn.execute(
                "INSERT INTO playlist_memberships (playlist_id, stable_id, "
                "position) VALUES (?, ?, ?)",
                (PLAYLIST_ID, sid, position),
            )
        writer.close()
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    audio_dir = tmp_path / "audio"
    master_path = tmp_path / "master.plain.db"
    state_path = tmp_path / "state.db"
    _make_master_plain_db(master_path, audio_dir)
    _make_state_db(state_path, audio_dir)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", master_path)
    monkeypatch.setattr(rb_config, "STATE_DB", state_path)
    return {"master": master_path, "state": state_path, "audio": audio_dir}


@pytest.fixture
def client(library: dict[str, Path]) -> Iterator[TestClient]:
    app = create_app(
        backend=SqliteBackend(library["state"]),
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(library["state"]),
        mount_frontend=False,
    )
    with TestClient(app) as test_client:
        yield test_client


def _playlist_rows(client: TestClient) -> dict[str, dict]:
    response = client.get(f"/api/v1/playlists/{PLAYLIST_ID}")
    assert response.status_code == 200, response.text
    return {row["stable_id"]: row for row in response.json()["tracks"]}


@pytest.mark.parametrize("stable_id", PLAYLIST_PLAYABLE_SIDS)
def test_playable_local_not_streaming_on_playlist(
    client: TestClient, stable_id: str
) -> None:
    """if a present local file is streaming on the playlist row then broken"""
    row = _playlist_rows(client)[stable_id]
    assert row["file_exists"] is True
    assert row["is_streaming"] is False


@pytest.mark.parametrize("stable_id", PLAYABLE_LOCAL_SIDS)
def test_playable_local_not_streaming_on_rb_meta(
    client: TestClient, stable_id: str
) -> None:
    """if a present local file is streaming on rb-meta then broken"""
    response = client.get(f"/api/v1/tracks/{stable_id}/rb-meta")
    assert response.status_code == 200, response.text
    meta = response.json()
    assert meta["vendor"] == "rekordbox"
    assert meta["file_exists"] is True
    assert meta["is_streaming"] is False


@pytest.mark.parametrize("stable_id", PLAYABLE_LOCAL_SIDS)
def test_playable_local_audio_loads(client: TestClient, stable_id: str) -> None:
    """if a present local file cannot serve /audio then broken"""
    response = client.get(f"/api/v1/tracks/{stable_id}/audio")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("audio/")


def test_true_streaming_stays_streaming(client: TestClient) -> None:
    """if a tidal row with no local file is not streaming then broken"""
    row = _playlist_rows(client)[TRUE_STREAMING]
    assert row["file_exists"] is False
    assert row["is_streaming"] is True

    response = client.get(f"/api/v1/tracks/{TRUE_STREAMING}/rb-meta")
    assert response.status_code == 200, response.text
    meta = response.json()
    assert meta["file_exists"] is False
    assert meta["is_streaming"] is True


def test_build_track_rows_pathless_present_not_streaming(
    library: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """if build_track_rows marks a pathless present track streaming then broken"""
    monkeypatch.setattr(track_rows.config, "STATE_DB", library["state"])
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", library["master"])
    rows = track_rows.build_track_rows([Track(stable_id=PATHLESS_PRESENT)])
    assert len(rows) == 1
    assert rows[0]["file_exists"] is True
    assert rows[0]["is_streaming"] is False
