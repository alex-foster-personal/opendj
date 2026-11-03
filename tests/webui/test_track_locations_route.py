"""Track location relink routes: POST/GET/DELETE /api/v1/tracks/{id}/locations.

[if] a track's recorded file is gone and POST attaches a real file under a music root
[then] the row reads back live and available, and playback resolves to that file, [else stop].
[if] DELETE removes that row [then] playback no longer resolves and the audio file is
untouched, and a re-POST revives the same location_id, [else stop].
[if] confirm is false, If-Match is missing or stale, or the file is missing, a symlink or
outside the music roots [then] the write is refused and nothing changes, [else stop].
[if] DELETE names another track's location or an unknown id [then] 404, [else stop].
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared import paths as shared_paths
from apps.shared.state import db as state_db
from apps.shared.state import locations as state_locations
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter
from apps.webui.server.app import create_app
from apps.webui.server.routes import playlist_write
from apps.webui.server.sqlite_backend import SqliteBackend

TRACK = "t-relink-target-0123456789012345678901234"
OTHER = "t-relink-other-01234567890123456789012345"


@pytest.fixture
def music_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "music"
    root.mkdir()
    monkeypatch.setattr(shared_paths, "MUSIC_ROOTS", [root])
    return root


@pytest.fixture
def new_file(music_root: Path) -> Path:
    path = music_root / "_ingest" / "Artist - Song.mp3"
    path.parent.mkdir()
    path.write_bytes(b"fixture-audio-bytes")
    return path


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    conn = state_db.open_rw(path)
    writer = StateWriter(conn, bus=FakeEventBus(), actor="test-seed")
    try:
        for sid in (TRACK, OTHER):
            writer.upsert_track(
                stable_id=sid,
                stable_id_tier="inferred",
                title=sid,
                artists=["Artist"],
                album=None,
                isrc=None,
                duration_ms=180_000,
                file_path=str(tmp_path / "gone" / f"{sid}.mp3"),
            )
    finally:
        writer.close()
        conn.close()
    return path


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


def _etag(client: TestClient, stable_id: str) -> str:
    r = client.get(f"/api/v1/tracks/{stable_id}")
    assert r.status_code == 200, r.text
    return r.headers["ETag"]


def _attach(client: TestClient, stable_id: str, path: Path, **extra: object):
    body = {"file_path": str(path), "confirm": True, **extra}
    return client.post(
        f"/api/v1/tracks/{stable_id}/locations",
        json=body,
        headers={"If-Match": _etag(client, stable_id)},
    )


def _playable(db_path: Path, stable_id: str) -> Path | None:
    conn = sqlite3.connect(str(db_path))
    try:
        return state_locations.local_audio_path(conn, stable_id)
    finally:
        conn.close()


def test_attach_makes_dead_track_playable_and_delete_undoes_it(
    client: TestClient, db_path: Path, new_file: Path,
) -> None:
    """[if] attach then delete [then] playable then not, file untouched, re-add revives, [else stop]."""
    assert _playable(db_path, TRACK) is None, "fixture track must start unplayable"
    mtime = new_file.stat().st_mtime_ns

    r = _attach(client, TRACK, new_file)
    assert r.status_code == 201, r.text
    loc = r.json()
    assert loc["role"] == "alternate" and loc["available"] is True
    assert loc["file_path"] == str(new_file.resolve())
    assert _playable(db_path, TRACK) == new_file.resolve()

    listed = client.get(f"/api/v1/tracks/{TRACK}/locations").json()["locations"]
    assert [x["location_id"] for x in listed if x["file_path"] == loc["file_path"]] == [loc["location_id"]]

    r = client.delete(
        f"/api/v1/tracks/{TRACK}/locations/{loc['location_id']}",
        headers={"If-Match": _etag(client, TRACK)},
    )
    assert r.status_code == 204, r.text
    assert _playable(db_path, TRACK) is None
    assert new_file.stat().st_mtime_ns == mtime
    listed = client.get(f"/api/v1/tracks/{TRACK}/locations").json()["locations"]
    assert loc["location_id"] not in [x["location_id"] for x in listed]

    again = _attach(client, TRACK, new_file)
    assert again.status_code == 201, again.text
    assert again.json()["location_id"] == loc["location_id"]
    assert _playable(db_path, TRACK) == new_file.resolve()


def test_attach_refuses_unconfirmed_unconditional_and_stale_writes(
    client: TestClient, db_path: Path, new_file: Path,
) -> None:
    """[if] confirm false, no If-Match, or stale If-Match [then] 422/428/409 and no row, [else stop]."""
    assert _attach(client, TRACK, new_file, confirm=False).status_code == 422
    r = client.post(
        f"/api/v1/tracks/{TRACK}/locations",
        json={"file_path": str(new_file), "confirm": True},
    )
    assert r.status_code == 428, r.text
    r = client.post(
        f"/api/v1/tracks/{TRACK}/locations",
        json={"file_path": str(new_file), "confirm": True},
        headers={"If-Match": '"stale"'},
    )
    assert r.status_code == 409, r.text
    assert _playable(db_path, TRACK) is None


def test_attach_refuses_missing_symlink_and_outside_root_files(
    client: TestClient, db_path: Path, new_file: Path, tmp_path: Path,
) -> None:
    """[if] the file is missing, a symlink, or outside the music roots [then] 422, [else stop]."""
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"x")
    link = new_file.parent / "link.mp3"
    link.symlink_to(new_file)
    for bad in (new_file.parent / "nope.mp3", link, outside):
        r = _attach(client, TRACK, bad)
        assert r.status_code == 422, (bad, r.text)
    assert _playable(db_path, TRACK) is None


def test_delete_refuses_other_tracks_and_unknown_locations(
    client: TestClient, new_file: Path,
) -> None:
    """[if] DELETE names another track's row or an unknown id [then] 404, [else stop]."""
    loc = _attach(client, TRACK, new_file).json()
    r = client.delete(
        f"/api/v1/tracks/{OTHER}/locations/{loc['location_id']}",
        headers={"If-Match": _etag(client, OTHER)},
    )
    assert r.status_code == 404, r.text
    r = client.delete(
        f"/api/v1/tracks/{TRACK}/locations/does-not-exist",
        headers={"If-Match": _etag(client, TRACK)},
    )
    assert r.status_code == 404, r.text
