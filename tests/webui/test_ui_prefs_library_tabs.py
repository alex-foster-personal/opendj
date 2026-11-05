"""LIBUX-8677: library browser tabs live in ui-prefs and agents drive them over HTTP.

[if] an agent or the browser lists, opens, switches, closes or PUTs library tabs [then] ui-prefs.json holds a valid tab set and the last tab never closes, [else stop].

Regression lines:
- if GET /api/v1/ui-prefs lacks library_tabs then a restart cannot restore tabs
- if POST /library-tabs open does not append and activate then agents cannot open a tab
- if close on the last tab succeeds then the browser is left with no list
- if switch to a missing index succeeds then agents get a silent no-op
- if a malformed library_tabs on disk loads then GET coerces instead of refusing
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

pytestmark = pytest.mark.requirement("LIBUX-8677")

TABS = "/api/v1/ui-prefs/library-tabs"


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


def _open(client: TestClient, playlist_id: str, name: str) -> dict:
    r = client.post(TABS, json={"action": "open", "playlist_id": playlist_id, "name": name})
    assert r.status_code == 200, r.text
    return r.json()["library_tabs"]


def test_defaults_have_no_saved_tabs(prefs_client: TestClient) -> None:
    body = prefs_client.get("/api/v1/ui-prefs").json()
    assert body["library_tabs"] == {"tabs": [], "active": 0}


def test_open_appends_and_activates_then_reopen_switches(prefs_client: TestClient) -> None:
    _open(prefs_client, "all", "All Tracks")
    state = _open(prefs_client, "pl-1", "Warmup")
    assert [t["playlist_id"] for t in state["tabs"]] == ["all", "pl-1"]
    assert state["active"] == 1
    state = _open(prefs_client, "all", "All Tracks")
    assert len(state["tabs"]) == 2 and state["active"] == 0


def test_open_refuses_a_fifth_tab(prefs_client: TestClient) -> None:
    for i in range(4):
        _open(prefs_client, f"pl-{i}", f"P{i}")
    r = prefs_client.post(TABS, json={"action": "open", "playlist_id": "pl-9", "name": "P9"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "LIBRARY_TABS_FULL"


def test_switch_and_close_keep_active_on_a_real_tab(prefs_client: TestClient) -> None:
    for i in range(3):
        _open(prefs_client, f"pl-{i}", f"P{i}")
    r = prefs_client.post(TABS, json={"action": "switch", "index": 1})
    assert r.json()["library_tabs"]["active"] == 1
    r = prefs_client.post(TABS, json={"action": "close", "index": 0})
    state = r.json()["library_tabs"]
    assert [t["playlist_id"] for t in state["tabs"]] == ["pl-1", "pl-2"]
    assert state["active"] == 0


def test_switch_out_of_range_is_refused(prefs_client: TestClient) -> None:
    _open(prefs_client, "pl-0", "P0")
    r = prefs_client.post(TABS, json={"action": "switch", "index": 3})
    assert r.status_code == 422


def test_last_tab_cannot_close(prefs_client: TestClient) -> None:
    _open(prefs_client, "pl-0", "P0")
    r = prefs_client.post(TABS, json={"action": "close", "index": 0})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "LIBRARY_TABS_LAST"


def test_list_returns_state_without_writing(prefs_client: TestClient) -> None:
    _open(prefs_client, "pl-0", "P0")
    r = prefs_client.post(TABS, json={"action": "list"})
    assert r.status_code == 200
    assert r.json()["library_tabs"]["tabs"][0]["name"] == "P0"


def test_put_round_trips_per_tab_sort_and_search(prefs_client: TestClient, tmp_path: Path) -> None:
    value = {
        "tabs": [
            {"playlist_id": "all", "name": "All Tracks", "search": "house", "sort_key": "bpm", "sort_dir": -1},
            {"playlist_id": None, "name": "blank list", "search": "", "sort_key": None, "sort_dir": 1},
        ],
        "active": 1,
    }
    r = prefs_client.put("/api/v1/ui-prefs", json={"library_tabs": value})
    assert r.status_code == 200, r.text
    on_disk = json.loads((tmp_path / "data" / "state" / "ui-prefs.json").read_text())
    assert on_disk["library_tabs"] == value
    assert prefs_client.get("/api/v1/ui-prefs").json()["library_tabs"] == value


def test_put_refuses_active_outside_tabs(prefs_client: TestClient) -> None:
    value = {
        "tabs": [{"playlist_id": "all", "name": "All", "search": "", "sort_key": None, "sort_dir": 1}],
        "active": 2,
    }
    r = prefs_client.put("/api/v1/ui-prefs", json={"library_tabs": value})
    assert r.status_code == 422


def test_malformed_tabs_on_disk_are_refused(prefs_client: TestClient, tmp_path: Path) -> None:
    path = tmp_path / "data" / "state" / "ui-prefs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"library_tabs": {"tabs": "nope", "active": 0}}))
    assert prefs_client.get("/api/v1/ui-prefs").status_code == 422
