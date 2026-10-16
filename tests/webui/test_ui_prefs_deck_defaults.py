"""SETSTUB-02: "Quantize on new decks" (`deck_default_quantize`) over HTTP.

The settings row writes this key, and the deck store seeds every fresh or
ejected deck's Quantize from it. Regression lines:
- if the key is absent from GET defaults then a fresh install has no value to seed from
- if the default is not True then every deck silently stops quantizing by default
- if a PUT does not land in ui-prefs.json then the setting is decorative
- if a wrong-typed value is accepted (PUT or on disk) then the deck seed gets a non-bool
- if writing it disturbs a neighbor key then the shared bool merge is wrong
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend

KEY = "deck_default_quantize"


@pytest.fixture
def prefs_client(tmp_path: Path) -> TestClient:
    data_dir = tmp_path / "data"
    state_path = data_dir / "state" / "state.db"
    state_db.open_rw(state_path).close()
    app = create_app(
        backend=SqliteBackend(state_path),
        bind_host="127.0.0.1",
        hostname="test-host",
        mount_frontend=False,
        state_db_path=str(state_path),
    )
    app.state.data_dir = data_dir
    with TestClient(app) as client:
        yield client


def _prefs_file(tmp_path: Path) -> Path:
    return tmp_path / "data" / "state" / "ui-prefs.json"


def test_deck_default_quantize_defaults_on_with_no_file(prefs_client: TestClient) -> None:
    body = prefs_client.get("/api/v1/ui-prefs").json()
    assert body[KEY] is True


def test_deck_default_quantize_defaults_on_for_a_blob_that_predates_it(
    prefs_client: TestClient, tmp_path: Path
) -> None:
    path = _prefs_file(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"theme": "dark", "confirm": {}}) + "\n", encoding="utf-8")
    assert prefs_client.get("/api/v1/ui-prefs").json()[KEY] is True


@pytest.mark.parametrize("value", [False, True])
def test_deck_default_quantize_put_round_trips_to_disk(prefs_client: TestClient, tmp_path: Path, value: bool) -> None:
    # Write the opposite first so the True case proves a real write, not the default.
    assert prefs_client.put("/api/v1/ui-prefs", json={KEY: not value}).status_code == 200
    r = prefs_client.put("/api/v1/ui-prefs", json={KEY: value})
    assert r.status_code == 200
    assert r.json()[KEY] is value
    assert json.loads(_prefs_file(tmp_path).read_text())[KEY] is value
    assert prefs_client.get("/api/v1/ui-prefs").json()[KEY] is value


def test_deck_default_quantize_put_leaves_neighbors_alone(prefs_client: TestClient) -> None:
    before = prefs_client.get("/api/v1/ui-prefs").json()
    assert prefs_client.put("/api/v1/ui-prefs", json={KEY: False}).status_code == 200
    after = prefs_client.get("/api/v1/ui-prefs").json()
    changed = sorted(k for k in after if after[k] != before[k])
    assert changed == [KEY]


def test_an_unrelated_put_does_not_reset_deck_default_quantize(prefs_client: TestClient) -> None:
    assert prefs_client.put("/api/v1/ui-prefs", json={KEY: False}).status_code == 200
    assert prefs_client.put("/api/v1/ui-prefs", json={"beat_sync_max": False}).status_code == 200
    assert prefs_client.get("/api/v1/ui-prefs").json()[KEY] is False


@pytest.mark.parametrize("bad", ["yes", 1, None])
def test_deck_default_quantize_put_rejects_non_boolean(prefs_client: TestClient, tmp_path: Path, bad: object) -> None:
    if bad is None:
        # null means "leave as stored" in this PATCH model, so it is not a 422;
        # prove it is a no-op instead of a reset.
        assert prefs_client.put("/api/v1/ui-prefs", json={KEY: False}).status_code == 200
        assert prefs_client.put("/api/v1/ui-prefs", json={KEY: None}).status_code == 200
        assert prefs_client.get("/api/v1/ui-prefs").json()[KEY] is False
        return
    assert prefs_client.put("/api/v1/ui-prefs", json={KEY: False}).status_code == 200
    r = prefs_client.put("/api/v1/ui-prefs", json={KEY: bad})
    assert r.status_code == 422
    # The refused write left the stored value alone (a coercion would read True).
    assert json.loads(_prefs_file(tmp_path).read_text())[KEY] is False


def test_deck_default_quantize_wrong_type_on_disk_is_refused(prefs_client: TestClient, tmp_path: Path) -> None:
    path = _prefs_file(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"theme": "dark", KEY: "yes"}) + "\n", encoding="utf-8")
    r = prefs_client.get("/api/v1/ui-prefs")
    assert r.status_code == 422
    assert KEY in json.dumps(r.json())
