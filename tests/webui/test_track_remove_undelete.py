"""LIBM-52 HTTP remove/undelete regression tests (also the server half of LIBM-78b:
the /reconcile admin view removes rows through this endpoint).

[if] POST :remove then POST :undelete on a track in two playlists [then] both
memberships return at original positions and the audio file is untouched,
[else stop].
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
def audio_path(db_path: Path) -> Path:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT file_path FROM tracks WHERE stable_id = ?",
            (TRACK_T,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return Path(row[0])


@pytest.fixture
def client(db_path: Path) -> Iterator[TestClient]:
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


@pytest.mark.requirement("LIBM-52")
def test_remove_then_undelete_in_two_playlists_leaves_file_untouched(
    client: TestClient,
    db_path: Path,
    audio_path: Path,
) -> None:
    """[if] a track in two playlists is removed then undeleted [then] memberships
    and listing visibility round-trip and the file mtime is unchanged, [else stop].

    [if] a track in two playlists is removed then undeleted [then] both round-trip, [else stop].
    """
    playlist_a, etag_a = _create_playlist(client, "Playlist A")
    playlist_b, etag_b = _create_playlist(client, "Playlist B")
    etag_a = _put_tracks(client, playlist_a["playlist_id"], [TRACK_T], etag_a)
    etag_b = _put_tracks(
        client,
        playlist_b["playlist_id"],
        [TRACK_OTHER, TRACK_T],
        etag_b,
    )

    before = audio_path.stat()
    remove = client.post(f"/api/v1/tracks/{TRACK_T}:remove")
    assert remove.status_code == 200, remove.text
    body = remove.json()
    assert body["stable_id"] == TRACK_T
    assert body["deleted_at"] is not None
    assert {(m["playlist_id"], m["position"]) for m in body["memberships"]} == {
        (playlist_a["playlist_id"], 0),
        (playlist_b["playlist_id"], 1),
    }

    conn = sqlite3.connect(str(db_path))
    try:
        track_deleted_at = conn.execute(
            "SELECT deleted_at FROM tracks WHERE stable_id = ?",
            (TRACK_T,),
        ).fetchone()[0]
        assert track_deleted_at == body["deleted_at"]
        for playlist_id, position in (
            (playlist_a["playlist_id"], 0),
            (playlist_b["playlist_id"], 1),
        ):
            membership_deleted_at = conn.execute(
                "SELECT deleted_at FROM playlist_memberships "
                "WHERE playlist_id = ? AND position = ?",
                (playlist_id, position),
            ).fetchone()[0]
            assert membership_deleted_at == body["deleted_at"]
        changelog_tables = {
            row[0] for row in conn.execute("SELECT DISTINCT table_name FROM local_changelog")
        }
    finally:
        conn.close()
    assert "tracks" in changelog_tables
    assert "playlist_memberships" in changelog_tables
    assert audio_path.exists()
    assert audio_path.stat().st_mtime_ns == before.st_mtime_ns

    listed = client.get("/api/v1/tracks")
    assert listed.status_code == 200
    assert TRACK_T not in {item["stable_id"] for item in listed.json()["items"]}
    listed_deleted = client.get("/api/v1/tracks", params={"show_deleted": True})
    assert TRACK_T in {item["stable_id"] for item in listed_deleted.json()["items"]}
    assert client.get(f"/api/v1/tracks/{TRACK_T}").status_code == 200

    playlist_a_detail = client.get(f"/api/v1/playlists/{playlist_a['playlist_id']}")
    playlist_b_detail = client.get(f"/api/v1/playlists/{playlist_b['playlist_id']}")
    assert TRACK_T not in playlist_a_detail.json()["items"]
    assert playlist_b_detail.json()["items"] == [TRACK_OTHER]

    undelete = client.post(f"/api/v1/tracks/{TRACK_T}:undelete")
    assert undelete.status_code == 200, undelete.text
    undelete_body = undelete.json()
    assert undelete_body["deleted_at"] is None
    assert {(m["playlist_id"], m["position"]) for m in undelete_body["memberships"]} == {
        (playlist_a["playlist_id"], 0),
        (playlist_b["playlist_id"], 1),
    }
    assert audio_path.exists()
    assert audio_path.stat().st_mtime_ns == before.st_mtime_ns

    listed_again = client.get("/api/v1/tracks")
    assert TRACK_T in {item["stable_id"] for item in listed_again.json()["items"]}
    playlist_a_after = client.get(f"/api/v1/playlists/{playlist_a['playlist_id']}")
    playlist_b_after = client.get(f"/api/v1/playlists/{playlist_b['playlist_id']}")
    assert playlist_a_after.json()["items"] == [TRACK_T]
    assert playlist_b_after.json()["items"] == [TRACK_OTHER, TRACK_T]


@pytest.mark.requirement("LIBM-52")
def test_remove_unknown_track_404(client: TestClient) -> None:
    """[if] :remove targets an unknown stable_id [then] 404 is returned, [else stop]."""
    r = client.post("/api/v1/tracks/does-not-exist:remove")
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "not_found"


@pytest.mark.requirement("LIBM-52")
def test_second_remove_409(client: TestClient) -> None:
    """[if] :remove is called twice [then] the second call is 409, [else stop]."""
    first = client.post(f"/api/v1/tracks/{TRACK_T}:remove")
    assert first.status_code == 200
    second = client.post(f"/api/v1/tracks/{TRACK_T}:remove")
    assert second.status_code == 409
    assert second.json()["detail"]["error"] == "already_removed"


@pytest.mark.requirement("LIBM-52")
def test_undelete_live_track_409(client: TestClient) -> None:
    """[if] :undelete targets a live track [then] 409 is returned, [else stop]."""
    r = client.post(f"/api/v1/tracks/{TRACK_T}:undelete")
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "not_removed"


@pytest.mark.requirement("LIBM-52")
def test_openapi_includes_remove_and_undelete_paths(db_path: Path) -> None:
    """[if] the OpenAPI schema is generated [then] :remove/:undelete paths exist, [else stop]."""
    app = create_app(
        backend=SqliteBackend(db_path),
        state_db_path=str(db_path),
        bind_host="127.0.0.1",
        hostname="test-host",
        lock_status_fn=lambda: None,
        mount_frontend=False,
    )
    paths = app.openapi()["paths"]
    assert "/api/v1/tracks/{stable_id}:remove" in paths
    assert "/api/v1/tracks/{stable_id}:undelete" in paths
