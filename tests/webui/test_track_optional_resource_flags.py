"""TrackOut optional-resource flags must predict absent lyrics, auto-cues, stems."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.lyrics.cache import LyricLine, Lyrics, cache_path, write
from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend


@pytest.fixture
def flags_client(tmp_path: Path) -> Iterator[TestClient]:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_db_path = state_dir / "state.db"
    connection = state_db.open_rw(state_db_path)
    connection.close()
    app = create_app(
        backend=SqliteBackend(state_db_path),
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(state_db_path),
        mount_frontend=False,
    )
    with TestClient(app) as client:
        yield client


def test_track_out_optional_resource_flags_on_empty_track(
    flags_client: TestClient, tmp_path: Path
) -> None:
    state_dir = tmp_path / "state"
    state_db_path = state_dir / "state.db"
    connection = state_db.open_rw(state_db_path)
    connection.execute(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("empty-track", "inferred", "Empty", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
    )
    connection.commit()
    connection.close()

    response = flags_client.get("/api/v1/tracks/empty-track")

    assert response.status_code == 200
    body = response.json()
    assert body["lyrics_available"] is False
    assert body["auto_cues_available"] is False
    assert body["stems_available"] is False

    lyrics_response = flags_client.get("/api/v1/tracks/empty-track/lyrics")
    assert lyrics_response.status_code == 404


def test_track_out_lyrics_available_when_cache_file_exists(
    flags_client: TestClient, tmp_path: Path
) -> None:
    state_dir = tmp_path / "state"
    state_db_path = state_dir / "state.db"
    connection = state_db.open_rw(state_db_path)
    connection.execute(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            "track-with-lyrics",
            "inferred",
            "Lyrics",
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:00Z",
        ),
    )
    connection.commit()
    connection.close()
    write(
        cache_path(tmp_path, "track-with-lyrics"),
        Lyrics(
            stable_id="track-with-lyrics",
            source="lrclib",
            lines=(LyricLine(start_ms=0, text="Line one"),),
        ),
    )

    response = flags_client.get("/api/v1/tracks/track-with-lyrics")

    assert response.status_code == 200
    assert response.json()["lyrics_available"] is True
