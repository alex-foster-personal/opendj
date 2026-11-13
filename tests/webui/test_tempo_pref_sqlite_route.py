"""Tempo pref PATCH against the real sqlite backend (PREF-2163).

Regression lines:
- if NaN or Infinity tempo values do not 422 then broken
- if a rejected non-finite value is persisted then broken
- if an empty or all-null tempo_pref object is not stored as null then broken
- if a real tempo_pref does not survive a reread then broken

[if] a PATCH sends a non-finite or empty tempo_pref [then] it is a 422 or a stored null, [else stop].
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend

pytestmark = pytest.mark.requirement("PREF-2163")

SID = "c" * 40
STAMP = "2026-11-13T00:00:00+00:00"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Build a disposable state.db holding one native track."""
    path = tmp_path / "state" / "state.db"
    path.parent.mkdir(parents=True)
    conn = state_db.open_rw(path)
    conn.execute(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, artists_json, file_path, "
        "created_at, updated_at) VALUES (?, 'inferred', 'tempo track', '[\"Artist\"]', "
        "'/music/t.mp3', ?, ?)",
        (SID, STAMP, STAMP),
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def client(db_path: Path) -> Iterator[TestClient]:
    """Serve the web UI over SqliteBackend on the disposable database."""
    app = create_app(
        backend=SqliteBackend(db_path),
        state_db_path=str(db_path),
        mount_frontend=False,
        enable_cors=False,
    )
    with TestClient(app) as test_client:
        yield test_client


def _etag(client: TestClient) -> str:
    """Fetch the track's current ETag for If-Match."""
    return client.get(f"/api/v1/tracks/{SID}").headers["ETag"]


def _stored_tempo_pref(db_path: Path) -> str | None:
    """Read the raw persisted tempo_pref field row, or None when absent."""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT value_json FROM track_fields WHERE stable_id = ? AND field_name = 'tempo_pref'",
            (SID,),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else row[0]


@pytest.mark.parametrize(
    "body",
    [
        '{"tempo_pref": {"regular": 1e400}}',
        '{"tempo_pref": {"min": -1e400}}',
        '{"tempo_pref": {"max": NaN}}',
        '{"tempo_pref": {"regular": Infinity}}',
    ],
)
def test_non_finite_tempo_pref_is_422_and_not_persisted(client: TestClient, db_path: Path, body: str) -> None:
    """[if] a tempo member is NaN or Infinity [then] 422 and nothing is written, [else stop]."""
    response = client.patch(
        f"/api/v1/tracks/{SID}",
        content=body,
        headers={"If-Match": _etag(client), "Content-Type": "application/json"},
    )
    assert response.status_code == 422, response.text
    assert _stored_tempo_pref(db_path) is None
    assert client.get(f"/api/v1/tracks/{SID}").json()["tempo_pref"] is None


@pytest.mark.parametrize("payload", [{}, {"regular": None, "min": None, "max": None}])
def test_empty_tempo_pref_object_is_stored_as_null(client: TestClient, db_path: Path, payload: dict[str, None]) -> None:
    """[if] tempo_pref is {} or all-null [then] the stored preference is null, [else stop]."""
    set_resp = client.patch(
        f"/api/v1/tracks/{SID}",
        json={"tempo_pref": {"regular": 140.0, "min": 130.0, "max": 150.0}},
        headers={"If-Match": _etag(client)},
    )
    assert set_resp.status_code == 200, set_resp.text
    assert set_resp.json()["tempo_pref"] == {"regular": 140.0, "min": 130.0, "max": 150.0}

    clear = client.patch(
        f"/api/v1/tracks/{SID}",
        json={"tempo_pref": payload},
        headers={"If-Match": _etag(client)},
    )
    assert clear.status_code == 200, clear.text
    assert clear.json()["tempo_pref"] is None
    assert client.get(f"/api/v1/tracks/{SID}").json()["tempo_pref"] is None
    assert _stored_tempo_pref(db_path) in (None, "null")
