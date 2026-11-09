"""DECKCTL-10421: Needle Lock (`deck_needle_lock`) over HTTP, the agent twin of the settings row.

[if] GET has no deck_needle_lock or it defaults on [then] fail, [else stop].
[if] a PUT of true does not land in ui-prefs.json and read back [then] fail, [else stop].
[if] a non-boolean is accepted [then] fail, [else stop].
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend

pytestmark = [pytest.mark.requirement("DECKCTL-10421")]

KEY = "deck_needle_lock"


@pytest.fixture
def prefs_client(tmp_path: Path) -> Iterator[TestClient]:
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


def test_needle_lock_defaults_off(prefs_client: TestClient) -> None:
    """[if] a fresh install reads Needle Lock on [then] fail."""
    assert prefs_client.get("/api/v1/ui-prefs").json()[KEY] is False


def test_needle_lock_put_persists_and_reads_back(prefs_client: TestClient, tmp_path: Path) -> None:
    """[if] PUT deck_needle_lock true does not persist or disturbs deck_default_quantize [then] fail."""
    response = prefs_client.put("/api/v1/ui-prefs", json={KEY: True})
    assert response.status_code == 200, response.text
    assert response.json()[KEY] is True
    on_disk = json.loads((tmp_path / "data" / "state" / "ui-prefs.json").read_text(encoding="utf-8"))
    assert on_disk[KEY] is True
    assert prefs_client.get("/api/v1/ui-prefs").json()["deck_default_quantize"] is True
    assert prefs_client.put("/api/v1/ui-prefs", json={KEY: False}).json()[KEY] is False


@pytest.mark.parametrize("bad", ["yes", 1, "true"])
def test_needle_lock_refuses_a_non_boolean(prefs_client: TestClient, bad: object) -> None:
    """[if] a coerced non-boolean is accepted [then] fail."""
    assert prefs_client.put("/api/v1/ui-prefs", json={KEY: bad}).status_code == 422
