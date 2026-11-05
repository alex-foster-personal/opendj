"""Works columns and the classical layout are off by default and persist (R-CLS-2, R-CLS-3)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.webui.server.routes import ui_prefs


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(ui_prefs, "DATA_DIR", tmp_path)
    app = FastAPI()
    app.include_router(ui_prefs.router)
    yield TestClient(app)


def test_a_fresh_install_shows_none_of_it(client: TestClient) -> None:
    body = client.get("/ui-prefs").json()
    assert body["library_optional_columns"] == []
    assert body["library_classical_layout"] is False


def test_optional_columns_persist_in_table_order(client: TestClient) -> None:
    client.put("/ui-prefs", json={"library_optional_columns": ["year", "work"]})
    assert client.get("/ui-prefs").json()["library_optional_columns"] == ["work", "year"]


def test_the_classical_layout_persists(client: TestClient) -> None:
    client.put("/ui-prefs", json={"library_classical_layout": True})
    assert client.get("/ui-prefs").json()["library_classical_layout"] is True


def test_an_unknown_column_is_refused(client: TestClient) -> None:
    response = client.put("/ui-prefs", json={"library_optional_columns": ["nope"]})
    assert response.status_code == 422


def test_a_non_boolean_layout_is_refused(client: TestClient) -> None:
    response = client.put("/ui-prefs", json={"library_classical_layout": "yes"})
    assert response.status_code == 422


def test_a_corrupt_prefs_file_fails_loudly(client: TestClient, tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / "ui-prefs.json").write_text(json.dumps({"library_optional_columns": "work"}))
    assert client.get("/ui-prefs").status_code == 422
