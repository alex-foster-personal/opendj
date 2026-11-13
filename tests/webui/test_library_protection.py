"""Library Protection (LIBM-57): scoped no-delete and no-edit locks over the playlist writes.

[if] a scoped lock is on [then] only that scope's playlist writes answer 423 Locked, [else stop].
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.opendj_cli import library_cli
from apps.shared.state import db as state_db
from apps.shared.state.events import FakeEventBus
from apps.shared.state.writer import StateWriter
from apps.webui.server.app import create_app
from apps.webui.server.library_protection import PROTECTION_FILENAME
from apps.webui.server.routes import playlist_write
from apps.webui.server.sqlite_backend import SqliteBackend

TRACK_A = "a" * 40
TRACK_B = "b" * 40
PROTECTION = "/api/v1/library/protection"

pytestmark = pytest.mark.requirement("LIBM-13834")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state" / "state.db"
    path.parent.mkdir()
    conn = state_db.open_rw(path)
    writer = StateWriter(conn, bus=FakeEventBus(), actor="test-seed")
    try:
        for sid, title in ((TRACK_A, "Track A"), (TRACK_B, "Track B")):
            writer.upsert_track(
                stable_id=sid,
                stable_id_tier="inferred",
                title=title,
                artists=["Artist"],
                album=None,
                isrc=None,
                duration_ms=180_000,
                file_path=None,
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


def _create(client: TestClient, name: str) -> tuple[str, str]:
    r = client.post("/api/v1/playlists", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["playlist_id"], r.headers["ETag"]


def _protect(client: TestClient, **locks: bool) -> None:
    r = client.put(PROTECTION, json=locks)
    assert r.status_code == 200, r.text


def _live_ids(client: TestClient) -> set[str]:
    return {row["playlist_id"] for row in client.get("/api/v1/playlists").json()}


def test_no_delete_refuses_playlist_delete_with_423(client: TestClient) -> None:
    """[if] no_delete is on and a playlist delete is sent [then] 423 Locked scope delete and the playlist survives, [else stop]."""
    playlist_id, etag = _create(client, "Keep me")
    _protect(client, no_delete=True)

    r = client.delete(f"/api/v1/playlists/{playlist_id}", headers={"If-Match": etag})

    assert r.status_code == 423, r.text
    assert r.json()["detail"]["error"] == "library_locked"
    assert r.json()["detail"]["scope"] == "delete"
    assert playlist_id in _live_ids(client)


def test_no_delete_still_allows_rename(client: TestClient) -> None:
    """[if] no_delete is on and a playlist rename is sent [then] the rename succeeds, [else stop]."""
    playlist_id, etag = _create(client, "Old name")
    _protect(client, no_delete=True)

    r = client.patch(f"/api/v1/playlists/{playlist_id}", json={"name": "New name"}, headers={"If-Match": etag})

    assert r.status_code == 200, r.text
    assert r.json()["name"] == "New name"


def test_no_edit_refuses_membership_add_with_423(client: TestClient) -> None:
    """[if] no_edit is on and a membership add is sent [then] 423 Locked scope edit and nothing is added, [else stop]."""
    playlist_id, _etag = _create(client, "Frozen")
    _protect(client, no_edit=True)

    r = client.post(f"/api/v1/playlists/{playlist_id}/items:add", json={"stable_ids": [TRACK_A]})

    assert r.status_code == 423, r.text
    assert r.json()["detail"]["scope"] == "edit"
    assert client.get(f"/api/v1/playlists/{playlist_id}").json()["items"] == []


def test_both_off_every_write_behaves_as_before(client: TestClient) -> None:
    """[if] both locks are off [then] rename, add, replace and delete all succeed as before, [else stop]."""
    _protect(client, no_delete=False, no_edit=False)
    playlist_id, etag = _create(client, "Open")

    rename = client.patch(f"/api/v1/playlists/{playlist_id}", json={"name": "Renamed"}, headers={"If-Match": etag})
    assert rename.status_code == 200, rename.text
    add = client.post(f"/api/v1/playlists/{playlist_id}/items:add", json={"stable_ids": [TRACK_A]})
    assert add.status_code == 200, add.text
    replace = client.put(
        f"/api/v1/playlists/{playlist_id}/tracks",
        json={"stable_ids": [TRACK_A, TRACK_B]},
        headers={"If-Match": add.headers["ETag"]},
    )
    assert replace.status_code == 200, replace.text
    delete = client.delete(f"/api/v1/playlists/{playlist_id}", headers={"If-Match": replace.headers["ETag"]})
    assert delete.status_code == 204, delete.text
    assert playlist_id not in _live_ids(client)


def test_protection_put_then_get_round_trips(client: TestClient, db_path: Path) -> None:
    """[if] the setting is PUT then GET [then] GET answers the PUT value and it is stored beside the state DB, [else stop]."""
    assert client.get(PROTECTION).json() == {"no_delete": False, "no_edit": False}

    put = client.put(PROTECTION, json={"no_edit": True})
    assert put.status_code == 200, put.text
    assert put.json() == {"no_delete": False, "no_edit": True}

    assert client.get(PROTECTION).json() == {"no_delete": False, "no_edit": True}
    stored = json.loads((db_path.parent / PROTECTION_FILENAME).read_text(encoding="utf-8"))
    assert stored == {"no_delete": False, "no_edit": True}


def test_protection_put_rejects_a_non_boolean_lock(client: TestClient) -> None:
    """[if] PUT sends a lock that is not a boolean [then] 422 and the stored setting is unchanged, [else stop]."""
    r = client.put(PROTECTION, json={"no_delete": "yes"})

    assert r.status_code == 422, r.text
    assert client.get(PROTECTION).json() == {"no_delete": False, "no_edit": False}


def test_corrupt_setting_fails_guarded_writes_instead_of_unlocking(client: TestClient, db_path: Path) -> None:
    """[if] the stored setting is not valid JSON [then] a guarded delete answers 500 and the playlist survives, [else stop]."""
    playlist_id, etag = _create(client, "Corrupt guard")
    (db_path.parent / PROTECTION_FILENAME).write_text("{not json", encoding="utf-8")

    r = client.delete(f"/api/v1/playlists/{playlist_id}", headers={"If-Match": etag})

    assert r.status_code == 500, r.text
    assert r.json()["detail"]["error"] == "library_protection_invalid"
    assert playlist_id in _live_ids(client)


def test_cli_sets_no_delete_and_the_route_reads_it(
    client: TestClient, db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] opendj library protection --no-delete on runs [then] it prints the result and GET reports no_delete on, [else stop]."""
    code = library_cli.run(["protection", "--no-delete", "on"], as_json=True, state_db=db_path)

    assert code == 0
    assert json.loads(capsys.readouterr().out) == {"no_delete": True, "no_edit": False}
    assert client.get(PROTECTION).json() == {"no_delete": True, "no_edit": False}


def test_cli_without_flags_prints_and_changes_nothing(db_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """[if] opendj library protection runs with no flag [then] it prints both locks off and writes no file, [else stop]."""
    code = library_cli.run(["protection"], as_json=False, state_db=db_path)

    assert code == 0
    assert capsys.readouterr().out.strip() == "no_delete off, no_edit off"
    assert not (db_path.parent / PROTECTION_FILENAME).exists()


def test_cli_rejects_a_bad_switch_and_writes_nothing(db_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """[if] opendj library protection --no-edit maybe runs [then] exit 1 with a usage error and no file is written, [else stop]."""
    code = library_cli.run(["protection", "--no-edit", "maybe"], as_json=True, state_db=db_path)

    assert code == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "usage"
    assert not (db_path.parent / PROTECTION_FILENAME).exists()
