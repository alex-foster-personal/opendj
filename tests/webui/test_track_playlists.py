"""LIBM-29 track playlist reverse lookup regression tests.

[if] GET /api/v1/tracks/{stable_id}/playlists is called for a track in K live
playlists [then] it returns exactly those K playlists and excludes tombstoned
memberships and deleted playlists, [else stop].
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter
from apps.webui.server.app import create_app
from apps.webui.server.routes import playlist_write
from apps.webui.server.sqlite_backend import SqliteBackend

TRACK_T = "t-target-track-id-012345678901234567890"
TRACK_OTHER = "t-other-track-id-01234567890123456789012"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    audio = tmp_path / "audio" / "target.flac"
    audio.parent.mkdir()
    audio.write_bytes(b"fixture-audio")
    conn = state_db.open_rw(path)
    writer = StateWriter(conn, bus=FakeEventBus(), actor="test-seed")
    try:
        for sid, title in ((TRACK_T, "Target"), (TRACK_OTHER, "Other")):
            writer.upsert_track(
                stable_id=sid,
                stable_id_tier="inferred",
                title=title,
                artists=["Artist"],
                album=None,
                isrc=None,
                duration_ms=180_000,
                file_path=str(audio) if sid == TRACK_T else None,
            )
    finally:
        writer.close()
        conn.close()
    return path


@pytest.fixture
def sqlite_client(db_path: Path) -> Iterator[TestClient]:
    app = create_app(
        backend=SqliteBackend(db_path),
        state_db_path=str(db_path),
        bind_host="127.0.0.1",
        hostname="test-host",
        lock_status_fn=lambda: None,
        mount_frontend=False,
    )
    with TestClient(app) as c:
        yield c
    playlist_write.close_store(app)


def _create_playlist(client: TestClient, name: str) -> tuple[dict, str]:
    r = client.post("/api/v1/playlists", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json(), r.headers["ETag"]


def _put_tracks(
    client: TestClient,
    playlist_id: str,
    stable_ids: list[str],
    etag: str,
) -> str:
    r = client.put(
        f"/api/v1/playlists/{playlist_id}/tracks",
        json={"stable_ids": stable_ids},
        headers={"If-Match": etag},
    )
    assert r.status_code == 200, r.text
    return r.headers["ETag"]


@pytest.mark.requirement("LIBM-29")
def test_track_in_two_live_playlists_plus_tombstoned_membership(
    sqlite_client: TestClient,
    db_path: Path,
) -> None:
    """[if] a track is in two live playlists and one tombstoned membership
    [then] GET playlists returns exactly two rows with correct positions,
    [else stop].

    [if] a track is in 2 live playlists plus 1 tombstoned one [then] GET returns 2, [else stop].
    """
    playlist_a, etag_a = _create_playlist(sqlite_client, "Playlist A")
    playlist_b, etag_b = _create_playlist(sqlite_client, "Playlist B")
    playlist_dead, etag_dead = _create_playlist(sqlite_client, "Playlist Dead")
    etag_a = _put_tracks(sqlite_client, playlist_a["playlist_id"], [TRACK_T], etag_a)
    etag_b = _put_tracks(
        sqlite_client,
        playlist_b["playlist_id"],
        [TRACK_OTHER, TRACK_T],
        etag_b,
    )
    _put_tracks(sqlite_client, playlist_dead["playlist_id"], [TRACK_T], etag_dead)

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE playlist_memberships SET deleted_at = ? "
            "WHERE playlist_id = ? AND stable_id = ?",
            ("2026-09-12T00:00:00Z", playlist_dead["playlist_id"], TRACK_T),
        )
        conn.commit()
    finally:
        conn.close()

    response = sqlite_client.get(f"/api/v1/tracks/{TRACK_T}/playlists")
    assert response.status_code == 200, response.text
    rows = response.json()
    assert len(rows) == 2
    by_id = {row["playlist_id"]: row for row in rows}
    assert playlist_dead["playlist_id"] not in by_id
    assert by_id[playlist_a["playlist_id"]]["name"] == "Playlist A"
    assert by_id[playlist_a["playlist_id"]]["vendor"] == "webui"
    assert by_id[playlist_a["playlist_id"]]["positions"] == [0]
    assert by_id[playlist_b["playlist_id"]]["name"] == "Playlist B"
    assert by_id[playlist_b["playlist_id"]]["positions"] == [1]


@pytest.mark.requirement("LIBM-29")
def test_unknown_stable_id_returns_404(sqlite_client: TestClient) -> None:
    """[if] the stable_id does not exist [then] GET playlists is 404 not [], [else stop]."""
    response = sqlite_client.get("/api/v1/tracks/no-such-stable-id/playlists")
    assert response.status_code == 404
    assert response.json() != []
    body = response.json()
    assert body.get("error") == "not_found"


@pytest.mark.requirement("LIBM-29")
def test_known_track_zero_memberships_returns_empty_list(sqlite_client: TestClient) -> None:
    """[if] a known track has no playlist memberships [then] GET playlists is
    200 [], [else stop].

    [if] a track has no memberships [then] GET playlists returns 200 [], [else stop].
    """
    response = sqlite_client.get(f"/api/v1/tracks/{TRACK_T}/playlists")
    assert response.status_code == 200, response.text
    assert response.json() == []


@pytest.mark.requirement("LIBM-29")
def test_deleted_playlist_excluded(sqlite_client: TestClient, db_path: Path) -> None:
    """[if] a third playlist holding the track is deleted [then] reverse lookup
    still returns only the two live playlists, [else stop].

    [if] a third playlist holding the track is deleted [then] lookup excludes it, [else stop].
    """
    playlist_a, etag_a = _create_playlist(sqlite_client, "Playlist A")
    playlist_b, etag_b = _create_playlist(sqlite_client, "Playlist B")
    playlist_c, etag_c = _create_playlist(sqlite_client, "Playlist C")
    etag_a = _put_tracks(sqlite_client, playlist_a["playlist_id"], [TRACK_T], etag_a)
    etag_b = _put_tracks(
        sqlite_client,
        playlist_b["playlist_id"],
        [TRACK_OTHER, TRACK_T],
        etag_b,
    )
    etag_c = _put_tracks(sqlite_client, playlist_c["playlist_id"], [TRACK_T], etag_c)

    tombstone = sqlite_client.delete(
        f"/api/v1/playlists/{playlist_c['playlist_id']}",
        headers={"If-Match": etag_c},
    )
    assert tombstone.status_code in (200, 204), tombstone.text

    response = sqlite_client.get(f"/api/v1/tracks/{TRACK_T}/playlists")
    assert response.status_code == 200, response.text
    rows = response.json()
    assert len(rows) == 2
    returned_ids = {row["playlist_id"] for row in rows}
    assert playlist_c["playlist_id"] not in returned_ids


@pytest.mark.requirement("LIBM-29")
def test_duplicate_positions_in_one_playlist(sqlite_client: TestClient) -> None:
    """[if] a track appears twice in one playlist [then] one row lists both
    positions sorted ascending, [else stop].

    [if] a track appears twice in one playlist [then] one row lists both positions, [else stop].
    """
    playlist, etag = _create_playlist(sqlite_client, "Dupes")
    etag = _put_tracks(
        sqlite_client,
        playlist["playlist_id"],
        [TRACK_T, TRACK_OTHER, TRACK_T],
        etag,
    )

    response = sqlite_client.get(f"/api/v1/tracks/{TRACK_T}/playlists")
    assert response.status_code == 200, response.text
    rows = response.json()
    assert len(rows) == 1
    assert rows[0]["playlist_id"] == playlist["playlist_id"]
    assert rows[0]["positions"] == [0, 2]


def test_in_memory_backend_track_playlists(client: TestClient) -> None:
    """Cheap proof the default InMemory app mounts list_track_playlists."""
    response = client.get("/api/v1/tracks/track-003/playlists")
    assert response.status_code == 200, response.text
    rows = response.json()
    assert len(rows) == 1
    assert rows[0]["playlist_id"] == "pl-001"
    assert rows[0]["name"] == "Opener Set"
    assert rows[0]["positions"] == [0]
