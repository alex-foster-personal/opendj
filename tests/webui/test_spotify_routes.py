"""Read-only HTTP contract for imported Spotify pending tracks."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.state import schema as state_schema
from apps.spotify.state_writer import ensure_aux_tables
from apps.webui.server.backend import InMemoryBackend, Playlist
from apps.webui.server.routes.spotify import router


def _make_client(
    tmp_path: Path,
    *,
    playlist: Playlist | None = None,
    with_pending_table: bool = True,
) -> tuple[TestClient, sqlite3.Connection]:
    """Mount only the Spotify router around a real, test-local state DB."""
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    state_schema.apply_migrations(conn)
    if with_pending_table:
        ensure_aux_tables(conn)

    backend = InMemoryBackend()
    if playlist is not None:
        backend.seed_playlist(playlist)

    app = FastAPI()
    app.state.backend = backend
    app.state.state_db_path = db_path
    app.include_router(router, prefix="/api/v1")
    return TestClient(app), conn


def _spotify_playlist() -> Playlist:
    return Playlist(
        playlist_id="spotify:pl123",
        name="Imported Spotify Playlist",
        vendor="spotify",
        vendor_pl_id="pl123",
    )


def _insert_pending(
    conn: sqlite3.Connection,
    *,
    position: int,
    status: str = "pending",
    sources: object | None = None,
) -> None:
    conn.execute(
        "INSERT INTO playlists "
        "(playlist_id, name, vendor, vendor_pl_id, created_at, updated_at) "
        "VALUES ('spotify:pl123', 'Imported Spotify Playlist', 'spotify', "
        "'pl123', '2026-07-22T00:00:00Z', '2026-07-22T00:00:00Z') "
        "ON CONFLICT(playlist_id) DO NOTHING"
    )
    conn.execute(
        "INSERT INTO pending_tracks "
        "(playlist_id, position, spotify_uri, isrc, title, artist, album, "
        "duration_ms, suggested_sources_json, status, added_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "spotify:pl123", position, f"spotify:track:{position}",
            "USABC2500001", f"Track {position}", "Artist", "Album", 123000,
            json.dumps(sources if sources is not None else {
                "beatport": "https://www.beatport.com/search?q=track",
                "bandcamp": "https://bandcamp.com/search?q=track",
                "qobuz": "https://www.qobuz.com/us-en/search?q=track",
                "apple_music": "https://music.apple.com/us/search?term=track",
                "discogs": "https://www.discogs.com/search?q=track",
            }),
            status,
            "2026-07-22T00:00:00Z",
        ),
    )


@pytest.mark.requirement("CAT-01")
def test_get_pending_tracks_returns_persisted_pending_rows_in_position_order(
    tmp_path: Path,
) -> None:
    client, conn = _make_client(tmp_path, playlist=_spotify_playlist())
    try:
        _insert_pending(conn, position=7)
        _insert_pending(conn, position=2)
        _insert_pending(conn, position=4, status="resolved")

        response = client.get("/api/v1/spotify/playlists/spotify:pl123/pending-tracks")

        assert response.status_code == 200
        rows = response.json()
        assert [row["position"] for row in rows] == [2, 7]
        assert rows[0]["spotify_uri"] == "spotify:track:2"
        assert rows[0]["suggested_sources"] == {
            "beatport": "https://www.beatport.com/search?q=track",
            "bandcamp": "https://bandcamp.com/search?q=track",
            "qobuz": "https://www.qobuz.com/us-en/search?q=track",
            "apple_music": "https://music.apple.com/us/search?term=track",
            "discogs": "https://www.discogs.com/search?q=track",
        }
    finally:
        client.close()
        conn.close()


@pytest.mark.requirement("CAT-01")
def test_get_pending_tracks_accepts_valid_source_keys_in_any_json_order(
    tmp_path: Path,
) -> None:
    client, conn = _make_client(tmp_path, playlist=_spotify_playlist())
    try:
        canonical_sources = {
            "beatport": "https://www.beatport.com/search?q=track",
            "bandcamp": "https://bandcamp.com/search?q=track",
            "qobuz": "https://www.qobuz.com/us-en/search?q=track",
            "apple_music": "https://music.apple.com/us/search?term=track",
            "discogs": "https://www.discogs.com/search?q=track",
        }
        _insert_pending(
            conn, position=0,
            sources=dict(reversed(list(canonical_sources.items()))),
        )

        response = client.get(
            "/api/v1/spotify/playlists/spotify:pl123/pending-tracks"
        )

        assert response.status_code == 200
        assert response.json()[0]["suggested_sources"] == canonical_sources
    finally:
        client.close()
        conn.close()


@pytest.mark.requirement("CAT-01")
@pytest.mark.parametrize("playlist", [None, Playlist(
    playlist_id="pl-001", name="Local Playlist", vendor="rekordbox",
)])
def test_get_pending_tracks_rejects_missing_or_non_spotify_playlist(
    tmp_path: Path,
    playlist: Playlist | None,
) -> None:
    client, conn = _make_client(tmp_path, playlist=playlist)
    try:
        response = client.get("/api/v1/spotify/playlists/pl-001/pending-tracks")

        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "SPOTIFY_PLAYLIST_NOT_FOUND"
    finally:
        client.close()
        conn.close()


@pytest.mark.requirement("CAT-01")
def test_get_pending_tracks_fails_when_auxiliary_table_is_absent(
    tmp_path: Path,
) -> None:
    client, conn = _make_client(
        tmp_path, playlist=_spotify_playlist(), with_pending_table=False,
    )
    try:
        response = client.get(
            "/api/v1/spotify/playlists/spotify:pl123/pending-tracks"
        )

        assert response.status_code == 500
        assert response.json()["detail"]["code"] == "PENDING_TRACKS_TABLE_MISSING"
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "pending_tracks" not in tables
    finally:
        client.close()
        conn.close()


@pytest.mark.requirement("CAT-01")
@pytest.mark.parametrize("sources", [
    {"beatport": "https://www.beatport.com/search?q=track"},
    {
        "beatport": "https://www.beatport.com/search?q=track",
        "bandcamp": "https://bandcamp.com/search?q=track",
        "qobuz": "https://www.qobuz.com/us-en/search?q=track",
        "apple_music": "https://music.apple.com/us/search?term=track",
        "discogs": 7,
    },
    "not-a-json-object",
])
def test_get_pending_tracks_fails_for_malformed_persisted_sources(
    tmp_path: Path,
    sources: object,
) -> None:
    client, conn = _make_client(tmp_path, playlist=_spotify_playlist())
    try:
        _insert_pending(conn, position=0, sources=sources)

        response = client.get(
            "/api/v1/spotify/playlists/spotify:pl123/pending-tracks"
        )

        assert response.status_code == 500
        assert response.json()["detail"]["code"] == "MALFORMED_PENDING_TRACK"
    finally:
        client.close()
        conn.close()
