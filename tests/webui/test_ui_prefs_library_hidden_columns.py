"""LIBUX-54: library column visibility persists in ui-prefs (library_hidden_columns).

The track table header's right-click chooser drives this key, so PUT
/api/v1/ui-prefs is the programmatic twin of every checkbox in it.

- [if] a fresh install GETs ui-prefs [then] library_hidden_columns is [], [else stop].
- [if] a hidden-column list is PUT [then] it lands in ui-prefs.json in table order, [else stop].
- [if] an unknown, repeated or unhashable id is sent or stored [then] the answer is 422, [else stop].
- [if] the server and browser column lists differ [then] fail, [else stop].
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.routes.ui_prefs import _LIBRARY_HIDEABLE_COLUMNS
from apps.webui.server.sqlite_backend import SqliteBackend

pytestmark = pytest.mark.requirement("LIBUX-54")

_REPO = Path(__file__).resolve().parents[2]
_TS_COLUMNS = _REPO / "apps/webui/frontend/src/lib/rb/library-columns.ts"


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


def test_hidden_columns_default_to_none(prefs_client: TestClient) -> None:
    body = prefs_client.get("/api/v1/ui-prefs").json()
    assert body["library_hidden_columns"] == []


def test_hidden_columns_round_trip_to_disk_in_table_order(
    prefs_client: TestClient, tmp_path: Path
) -> None:
    put = prefs_client.put("/api/v1/ui-prefs", json={"library_hidden_columns": ["stems", "key"]})
    assert put.status_code == 200, put.text
    assert put.json()["library_hidden_columns"] == ["key", "stems"]
    on_disk = json.loads((tmp_path / "data" / "state" / "ui-prefs.json").read_text())
    assert on_disk["library_hidden_columns"] == ["key", "stems"]
    assert prefs_client.get("/api/v1/ui-prefs").json()["library_hidden_columns"] == ["key", "stems"]


def test_showing_every_column_again_stores_an_empty_list(prefs_client: TestClient) -> None:
    prefs_client.put("/api/v1/ui-prefs", json={"library_hidden_columns": ["key"]})
    put = prefs_client.put("/api/v1/ui-prefs", json={"library_hidden_columns": []})
    assert put.json()["library_hidden_columns"] == []


@pytest.mark.parametrize(
    "bad",
    [["title"], ["lyrics"], ["key", "key"], "key", [1]],
    ids=["title-not-hideable", "lyrics-has-own-switch", "duplicate", "not-a-list", "not-a-string"],
)
def test_put_refuses_a_bad_hidden_column_list(prefs_client: TestClient, bad: object) -> None:
    put = prefs_client.put("/api/v1/ui-prefs", json={"library_hidden_columns": bad})
    assert put.status_code == 422, put.text
    assert prefs_client.get("/api/v1/ui-prefs").json()["library_hidden_columns"] == []


@pytest.mark.parametrize(
    "bad",
    [["key", "nope"], [{"bad": True}], [["key"]], ["key", "key"]],
    ids=["unknown-id", "unhashable-dict", "unhashable-list", "duplicate"],
)
def test_get_refuses_a_bad_list_on_disk(prefs_client: TestClient, tmp_path: Path, bad: object) -> None:
    """Every malformed list is a 422, never a 500 (an unhashable entry once raised TypeError)."""
    prefs_client.put("/api/v1/ui-prefs", json={"library_density": "cosy"})
    path = tmp_path / "data" / "state" / "ui-prefs.json"
    stored = json.loads(path.read_text())
    stored["library_hidden_columns"] = bad
    path.write_text(json.dumps(stored))
    got = prefs_client.get("/api/v1/ui-prefs")
    assert got.status_code == 422, got.text
    assert got.json()["detail"]["code"] == "UI_PREFS_INVALID"


def test_server_and_browser_list_the_same_columns_in_the_same_order() -> None:
    source = _TS_COLUMNS.read_text(encoding="utf-8")
    block = re.search(r"export const HIDEABLE_COLUMNS = \[(.*?)\] as const", source, re.S)
    assert block is not None, "HIDEABLE_COLUMNS not found in library-columns.ts"
    browser = tuple(re.findall(r"'([a-z]+)'", block.group(1)))
    assert browser == _LIBRARY_HIDEABLE_COLUMNS
