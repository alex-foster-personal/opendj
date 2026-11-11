"""Per-track Hide in tracklists user field (NOWP-11487).

Regression lines:
- if PATCH tracklist_hidden does not survive reopen then broken
- if unknown stable_id is not 404 then broken
- if yes, 1 or null do not 422 then broken
- if redact_identity keeps stable_id, key, bpm and since then broken

[if] a hidden track is listed in a tracklist export [then] it reads ID - ID, [else stop].
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.shared.state.tracklist_hidden import hidden_stable_ids, redact_identity
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend

pytestmark = pytest.mark.requirement("NOWP-11487")

SID = "a" * 40
RB_SID = "b" * 40
UNKNOWN = "9" * 40
STAMP = "2026-11-10T00:00:00+00:00"


@pytest.fixture
def library(tmp_path: Path) -> tuple[Path, Path]:
    db_path = tmp_path / "state" / "state.db"
    db_path.parent.mkdir(parents=True)
    conn = state_db.open_rw(db_path)
    for stable_id, title in ((SID, "native title"), (RB_SID, "rb title")):
        conn.execute(
            "INSERT INTO tracks (stable_id, stable_id_tier, title, artists_json, file_path, "
            "created_at, updated_at) VALUES (?, 'inferred', ?, '[\"Artist\"]', '/music/x.mp3', ?, ?)",
            (stable_id, title, STAMP, STAMP),
        )
    conn.execute(
        "INSERT INTO track_vendor_ids (stable_id, vendor, vendor_id) VALUES (?, 'rekordbox', '42')",
        (RB_SID,),
    )
    conn.commit()
    conn.close()
    return db_path, tmp_path


@pytest.fixture
def client(library: tuple[Path, Path]) -> Iterator[TestClient]:
    db_path, _tmp = library
    app = create_app(
        backend=SqliteBackend(db_path),
        state_db_path=str(db_path),
        mount_frontend=False,
        enable_cors=False,
    )
    with TestClient(app) as test_client:
        yield test_client


def _etag(client: TestClient, stable_id: str) -> str:
    return client.get(f"/api/v1/tracks/{stable_id}").headers["ETag"]


def test_patch_survives_reopen(client: TestClient, library: tuple[Path, Path]) -> None:
    etag = _etag(client, SID)
    hide = client.patch(
        f"/api/v1/tracks/{SID}",
        json={"tracklist_hidden": True},
        headers={"If-Match": etag},
    )
    assert hide.status_code == 200, hide.text
    body = hide.json()
    assert body["tracklist_hidden"] is True
    assert body["provenance"]["tracklist_hidden"]["source"] == "webui"

    db_path, _tmp = library
    app2 = create_app(
        backend=SqliteBackend(db_path),
        state_db_path=str(db_path),
        mount_frontend=False,
        enable_cors=False,
    )
    with TestClient(app2) as reopened:
        assert reopened.get(f"/api/v1/tracks/{SID}").json()["tracklist_hidden"] is True
        etag2 = _etag(reopened, SID)
        show = reopened.patch(
            f"/api/v1/tracks/{SID}",
            json={"tracklist_hidden": False},
            headers={"If-Match": etag2},
        )
        assert show.status_code == 200
        assert show.json()["tracklist_hidden"] is False


@pytest.mark.parametrize("payload", ["yes", 1, None])
def test_invalid_patch_values_422(client: TestClient, payload: object) -> None:
    etag = _etag(client, SID)
    response = client.patch(
        f"/api/v1/tracks/{SID}",
        json={"tracklist_hidden": payload},
        headers={"If-Match": etag},
    )
    assert response.status_code == 422, response.text


def test_unknown_track_404(client: TestClient) -> None:
    response = client.patch(
        f"/api/v1/tracks/{UNKNOWN}",
        json={"tracklist_hidden": True},
        headers={"If-Match": '"bogus"'},
    )
    assert response.status_code == 404, response.text


def test_rekordbox_mapped_track_keeps_vendor_title(client: TestClient, library: tuple[Path, Path]) -> None:
    etag = _etag(client, RB_SID)
    response = client.patch(
        f"/api/v1/tracks/{RB_SID}",
        json={"tracklist_hidden": True},
        headers={"If-Match": etag},
    )
    assert response.status_code == 200, response.text
    track = response.json()
    assert track["title"] == "rb title"
    assert track["artist"] == "Artist"
    db_path, _tmp = library
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT value_json FROM track_fields WHERE stable_id = ? AND field_name = 'tracklist_hidden'",
            (RB_SID,),
        ).fetchone()
        assert row is not None
        assert hidden_stable_ids(conn) == frozenset({RB_SID})
    finally:
        conn.close()


def test_hidden_stable_ids_and_redact_identity() -> None:
    row = {
        "stable_id": SID,
        "artist": "Secret Artist",
        "title": "Secret Title",
        "key": "8A",
        "bpm": 128.0,
        "since": "2026-11-10T00:00:00+00:00",
    }
    redacted = redact_identity(row)
    assert redacted["artist"] == "ID"
    assert redacted["title"] == "ID"
    assert redacted["stable_id"] == SID
    assert redacted["key"] == "8A"
    assert redacted["bpm"] == 128.0
    assert redacted["since"] == row["since"]
