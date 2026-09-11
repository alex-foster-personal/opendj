"""TrackOut optional-resource flags must predict absent lyrics, auto-cues, stems, artwork."""

from __future__ import annotations

import wave
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
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
    assert body["artwork_available"] is False

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


def _write_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(44_100)
        output.writeframes(b"\x00\x00" * 200)


def test_track_out_artwork_available_agrees_with_artwork_route(
    flags_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wav_path = tmp_path / "no-art.wav"
    _write_wav(wav_path)
    state_dir = tmp_path / "state"
    state_db_path = state_dir / "state.db"
    monkeypatch.setattr(rb_config, "STATE_DB", state_db_path)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", tmp_path / "absent.db")
    connection = state_db.open_rw(state_db_path)
    stable_id = "a" * 40
    connection.execute(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, file_path, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            stable_id,
            "inferred",
            "No Art",
            str(wav_path),
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:00Z",
        ),
    )
    connection.commit()
    connection.close()

    track_resp = flags_client.get(f"/api/v1/tracks/{stable_id}")
    assert track_resp.status_code == 200
    artwork_available = track_resp.json()["artwork_available"]
    assert artwork_available is not True

    artwork_resp = flags_client.get(f"/api/v1/tracks/{stable_id}/artwork")
    if artwork_available is True:
        assert artwork_resp.status_code == 200
    elif artwork_available is False:
        assert artwork_resp.status_code == 404
    elif artwork_available is None:
        assert artwork_resp.status_code == 503
        detail = artwork_resp.json()["detail"]
        assert detail["code"] == "ARTWORK_READER_UNAVAILABLE"
