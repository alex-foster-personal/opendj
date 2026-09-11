"""Contract tests for the cached line-synced lyrics read route."""

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
def lyrics_client(tmp_path: Path) -> Iterator[TestClient]:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_db_path = state_dir / "state.db"
    connection = state_db.open_rw(state_db_path)
    connection.close()
    write(
        cache_path(tmp_path, "track-with-lyrics"),
        Lyrics(
            stable_id="track-with-lyrics",
            source="lrclib",
            lines=(
                LyricLine(start_ms=0, text="First line"),
                LyricLine(start_ms=1_250, text="Second line"),
            ),
        ),
    )
    app = create_app(
        backend=SqliteBackend(state_db_path),
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(state_db_path),
        mount_frontend=False,
    )
    with TestClient(app) as client:
        yield client


def test_read_lyrics_returns_the_cached_line_timeline(lyrics_client: TestClient) -> None:
    response = lyrics_client.get("/api/v1/tracks/track-with-lyrics/lyrics")

    assert response.status_code == 200
    assert response.json() == {
        "stable_id": "track-with-lyrics",
        "source": "lrclib",
        "lines": [
            {"start_ms": 0, "text": "First line"},
            {"start_ms": 1250, "text": "Second line"},
        ],
    }


def test_read_lyrics_returns_404_when_no_cached_lyrics_exist(lyrics_client: TestClient) -> None:
    response = lyrics_client.get("/api/v1/tracks/no-lyrics/lyrics")

    assert response.status_code == 404
    assert response.json()["detail"] == "no cached lyrics for 'no-lyrics'"


def test_lyrics_cached_ids_lists_only_valid_cache_entries(lyrics_client: TestClient) -> None:
    response = lyrics_client.get("/api/v1/tracks/lyrics-cached-ids")

    assert response.status_code == 200
    assert response.json() == {"stable_ids": ["track-with-lyrics"]}


def test_lyrics_cached_ids_is_empty_when_no_cache_entries_exist(tmp_path: Path) -> None:
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
        response = client.get("/api/v1/tracks/lyrics-cached-ids")

    assert response.status_code == 200
    assert response.json() == {"stable_ids": []}


def test_read_lyrics_fails_when_the_app_has_no_configured_state_path(
    lyrics_client: TestClient,
) -> None:
    del lyrics_client.app.state.state_db_path

    with pytest.raises(AttributeError, match="state_db_path"):
        lyrics_client.get("/api/v1/tracks/track-with-lyrics/lyrics")


def test_openapi_declares_the_agent_native_lyrics_read_route() -> None:
    operation = create_app(mount_frontend=False).openapi()["paths"][
        "/api/v1/tracks/{stable_id}/lyrics"
    ]["get"]

    assert operation["responses"]["404"]["content"]["application/json"]["schema"]["$ref"] == (
        "#/components/schemas/LyricsUnavailableOut"
    )


pytestmark = pytest.mark.rb_parity
