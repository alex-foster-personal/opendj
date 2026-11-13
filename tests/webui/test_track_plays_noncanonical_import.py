"""GET /tracks/{sid}/plays with a non-canonical imported Last Played (#13857).

rekordbox imports can carry a date-only or timezone-less ``last_played_at``.
``parse_canonical`` rejects those with ``SyncStampError``; the route must keep
answering 200 and fall back to the Open DJ play-log value.

[if] the imported Last Played is not canonical [then] the plays route answers 200 with the play-log value, [else stop].

Regression one-liners:
  - if a date-only imported last_played_at 500s the plays route then broken
  - if a timezone-less imported value with no play-log entry raises then broken
  - if two canonical values no longer return the later one then broken
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.shared.state import db as state_db
from apps.webui.server.routes.track_plays import router
from apps.webui.server.sqlite_backend import make_backend

pytestmark = [pytest.mark.requirement("LIBM-13889")]

SID = "e" * 40


def _seed_state(path: Path) -> None:
    conn = state_db.open_rw(path)
    try:
        conn.execute(
            "INSERT INTO tracks (stable_id, stable_id_tier, title, created_at, updated_at) "
            "VALUES (?, 'inferred', 'imported', '2026-01-01', '2026-01-01')",
            (SID,),
        )
        conn.commit()
    finally:
        conn.close()


def _import_last_played(path: Path, value: str) -> None:
    conn = state_db.open_rw(path)
    try:
        conn.execute(
            "INSERT INTO track_fields (stable_id, field_name, value_json, source, modified_at) "
            "VALUES (?, 'last_played_at', ?, 'rekordbox', '2026-01-01T00:00:00.000000+00:00')",
            (SID, json.dumps(value)),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def state_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state.db"
    _seed_state(state)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", tmp_path / "absent-master.db")
    monkeypatch.setattr(rb_config, "STATE_DB", state)
    return state


@pytest.fixture
def client(state_path: Path) -> Iterator[TestClient]:
    app = FastAPI()
    app.state.backend = make_backend()
    app.state.state_db_path = state_path
    app.include_router(router, prefix="/api/v1")
    with TestClient(app, raise_server_exceptions=True) as test_client:
        yield test_client


def _log_play(client: TestClient) -> dict[str, object]:
    resp = client.post(
        f"/api/v1/tracks/{SID}/plays",
        json={"play_id": "load-noncanon-1", "audible_s": 75.0, "deck": 1},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_date_only_import_with_play_log_returns_play_log_value(client: TestClient, state_path: Path) -> None:
    """[if] imported last-played is date-only and a play-log entry exists [then] 200 with the play-log value, [else stop]."""
    _import_last_played(state_path, "2024-05-01")
    logged = _log_play(client)
    resp = client.get(f"/api/v1/tracks/{SID}/plays")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["opendj_last_played_at"] is not None
    assert body["last_played_at"] == body["opendj_last_played_at"]
    assert body["last_played_at"] == logged["opendj_last_played_at"]


def test_timezone_less_import_without_play_log_answers_200(client: TestClient, state_path: Path) -> None:
    """[if] imported value is timezone-less and no play-log entry exists [then] 200 and no raise, [else stop]."""
    naive = "2024-05-01T21:30:00"
    _import_last_played(state_path, naive)
    resp = client.get(f"/api/v1/tracks/{SID}/plays")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["opendj_play_count"] == 0
    assert body["last_played_at"] == naive


def test_timezone_less_import_with_play_log_returns_play_log_value(client: TestClient, state_path: Path) -> None:
    """[if] imported value is timezone-less and a play is logged [then] 200 with the play-log value, [else stop]."""
    _import_last_played(state_path, "2024-05-01T21:30:00")
    _log_play(client)
    body = client.get(f"/api/v1/tracks/{SID}/plays").json()
    assert body["last_played_at"] == body["opendj_last_played_at"]


@pytest.mark.parametrize(
    ("imported", "imported_wins"),
    [("2999-01-01T00:00:00.000000+00:00", True), ("2001-01-01T00:00:00.000000+00:00", False)],
)
def test_both_canonical_returns_the_later_one(
    client: TestClient, state_path: Path, imported: str, imported_wins: bool
) -> None:
    """[if] both imported and play-log values are canonical [then] the later one is returned, [else stop]."""
    _import_last_played(state_path, imported)
    _log_play(client)
    body = client.get(f"/api/v1/tracks/{SID}/plays").json()
    expected = imported if imported_wins else body["opendj_last_played_at"]
    assert body["last_played_at"] == expected
