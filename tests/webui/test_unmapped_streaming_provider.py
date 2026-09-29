"""Listing rows name their streaming provider even with no rekordbox mapping.

CHROME-02 (Codex P2 on PR #3896): a djay-only or locally imported streaming
row has no rekordbox mapping, so the browser never loads its rb-meta and never
sees a FolderPath. The row itself must carry the provider, or the table falls
back to the generic cloud icon.

[if] an unmapped tidal:/soundcloud: row is listed [then] its row carries that
provider, [else stop].
[if] a local file row is listed [then] it carries no provider, [else stop].

Real SQLite fixtures through the real app and the real row builder; the only
override is pointing config at those fixture databases, as the neighboring
test_pathless_local_not_streaming.py does.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.shared import platform_paths
from apps.shared.state import db as state_db
from apps.shared.state.writer import StateWriter
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend

pytestmark = [pytest.mark.requirement("CHROME-02")]

UNMAPPED_TIDAL = "a" * 40
UNMAPPED_SOUNDCLOUD = "b" * 40
UNMAPPED_HTTP = "c" * 40
UNMAPPED_LOCAL = "d" * 40
# A stale streaming URI whose audio IS present locally: not streaming, so no provider.
UNMAPPED_URI_PRESENT = "e" * 40
PLAYLIST_ID = "pl-unmapped-streaming"


def _make_master_plain_db(path: Path) -> None:
    # Empty rekordbox library: none of the rows below has a mapping.
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
        conn.commit()
    finally:
        conn.close()


def _make_state_db(path: Path, audio_dir: Path) -> None:
    audio_dir.mkdir(parents=True, exist_ok=True)
    local_file = audio_dir / "local.mp3"
    local_file.write_bytes(b"ID3" + bytes(1024))
    conn = state_db.open_rw(path)
    try:
        writer = StateWriter(conn, actor="unit-test")
        specs = [
            (UNMAPPED_TIDAL, "tidal:track:123"),
            (UNMAPPED_SOUNDCLOUD, "soundcloud:tracks:456"),
            (UNMAPPED_HTTP, "https://example.com/stream.mp3"),
            (UNMAPPED_LOCAL, str(local_file)),
            (UNMAPPED_URI_PRESENT, "spotify:track:now-local"),
        ]
        for sid, file_path in specs:
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
        writer.upsert_track_location(
            stable_id=UNMAPPED_URI_PRESENT, kind="local", file_path=str(local_file)
        )
        conn.execute(
            "INSERT INTO playlists (playlist_id, name, vendor, vendor_pl_id, "
            "created_at, updated_at) VALUES (?, 'Unmapped streaming', 'djay', "
            "'djay-pl-1', '2026-01-01', '2026-01-01')",
            (PLAYLIST_ID,),
        )
        for position, (sid, _path) in enumerate(specs):
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
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    master_path = tmp_path / "master.plain.db"
    state_path = tmp_path / "state.db"
    _make_master_plain_db(master_path)
    _make_state_db(state_path, tmp_path / "audio")
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", master_path)
    monkeypatch.setattr(rb_config, "STATE_DB", state_path)
    app = create_app(
        backend=SqliteBackend(state_path),
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(state_path),
        mount_frontend=False,
    )
    with TestClient(app) as test_client:
        yield test_client


def _rows(client: TestClient) -> dict[str, dict]:
    response = client.get(f"/api/v1/playlists/{PLAYLIST_ID}")
    assert response.status_code == 200, response.text
    return {row["stable_id"]: row for row in response.json()["tracks"]}


@pytest.mark.parametrize(
    ("stable_id", "provider"),
    [(UNMAPPED_TIDAL, "tidal"), (UNMAPPED_SOUNDCLOUD, "soundcloud"), (UNMAPPED_HTTP, "unknown")],
)
def test_unmapped_streaming_row_carries_its_provider(
    client: TestClient, stable_id: str, provider: str
) -> None:
    """if an unmapped streaming row loses its provider then broken"""
    row = _rows(client)[stable_id]
    assert row["has_rb_mapping"] is False, "the case under test is an UNMAPPED row"
    assert row["is_streaming"] is True
    assert row["streaming_provider"] == provider


def test_local_row_carries_no_provider(client: TestClient) -> None:
    """control: a present local file is not streaming and names no provider"""
    row = _rows(client)[UNMAPPED_LOCAL]
    assert row["has_rb_mapping"] is False
    assert row["file_exists"] is True
    assert row["is_streaming"] is False
    assert row["streaming_provider"] is None


def test_present_audio_behind_a_streaming_uri_names_no_provider(client: TestClient) -> None:
    """control: the provider follows is_streaming, not the bare URI prefix"""
    row = _rows(client)[UNMAPPED_URI_PRESENT]
    assert row["has_rb_mapping"] is False
    assert row["file_exists"] is True
    assert row["is_streaming"] is False
    assert row["streaming_provider"] is None


@pytest.mark.parametrize(
    ("path", "provider"),
    [
        ("spotify:track:x", "spotify"),
        ("tidal:track:x", "tidal"),
        ("soundcloud:tracks:x", "soundcloud"),
        ("http://example.com/a", "unknown"),
        ("https://example.com/a", "unknown"),
        ("/music/a.mp3", None),
        ("", None),
        (None, None),
    ],
)
def test_streaming_provider_parses_the_shared_prefix_set(path: str | None, provider: str | None) -> None:
    """if a non-streaming path names a provider, or a streaming URI names none, then broken"""
    assert platform_paths.streaming_provider(path) == provider
