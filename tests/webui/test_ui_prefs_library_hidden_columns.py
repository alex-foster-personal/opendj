"""LIBUX-54: library column visibility persists in ui-prefs (library_hidden_columns).

The track table header's right-click chooser drives this key, so PUT
/api/v1/ui-prefs is the programmatic twin of every checkbox in it.

- [if] a fresh install GETs ui-prefs [then] library_hidden_columns hides album, file path, date added, bitrate, kind, last played, [else stop].
- [if] album is PUT hidden or shown [then] it persists through the next GET, [else stop].
- [if] date_added is PUT shown or hidden [then] it persists through the next GET, [else stop].
- [if] bitrate or kind is PUT shown or hidden [then] it persists through the next GET, [else stop].
- [if] last_played is PUT shown or hidden [then] it persists through the next GET, [else stop].
- [if] filename or location is PUT hidden [then] it persists through the next GET, [else stop].
- [if] a hidden-column list is PUT [then] it lands in ui-prefs.json in table order, [else stop].
- [if] an unknown, repeated or unhashable id is sent or stored [then] the answer is 422, [else stop].
- [if] the server and browser column lists differ [then] fail, [else stop].
- [if] Tags is PUT as hidden [then] it persists through the next GET, [else stop].
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


@pytest.mark.requirement("LIBUX-10719")
def test_hidden_columns_default_hides_file_path_columns(prefs_client: TestClient) -> None:
    """[if] a fresh install GETs ui-prefs [then] album, file path, date added, bitrate, kind, last played start hidden, [else stop]."""
    body = prefs_client.get("/api/v1/ui-prefs").json()
    assert body["library_hidden_columns"] == ["album", "filename", "location", "date_added", "bitrate", "kind", "last_played"]


@pytest.mark.requirement("LIBUX-13747")
def test_album_column_is_hideable_over_http(prefs_client: TestClient, tmp_path: Path) -> None:
    """[if] album is PUT shown then hidden [then] both persist to disk in table order, [else stop]."""
    shown = prefs_client.put("/api/v1/ui-prefs", json={"library_hidden_columns": ["key"]})
    assert shown.status_code == 200, shown.text
    assert "album" not in shown.json()["library_hidden_columns"]
    hidden = prefs_client.put("/api/v1/ui-prefs", json={"library_hidden_columns": ["key", "album"]})
    assert hidden.status_code == 200, hidden.text
    assert hidden.json()["library_hidden_columns"] == ["album", "key"]
    on_disk = json.loads((tmp_path / "data" / "state" / "ui-prefs.json").read_text(encoding="utf-8"))
    assert on_disk["library_hidden_columns"] == ["album", "key"]
    assert prefs_client.get("/api/v1/ui-prefs").json()["library_hidden_columns"] == ["album", "key"]


@pytest.mark.requirement("LIBUX-13803")
def test_date_added_column_is_hideable_over_http(prefs_client: TestClient, tmp_path: Path) -> None:
    """[if] date_added is PUT shown then hidden [then] both persist to disk in table order, [else stop]."""
    shown = prefs_client.put("/api/v1/ui-prefs", json={"library_hidden_columns": ["album"]})
    assert shown.status_code == 200, shown.text
    assert "date_added" not in shown.json()["library_hidden_columns"]
    hidden = prefs_client.put("/api/v1/ui-prefs", json={"library_hidden_columns": ["date_added", "key"]})
    assert hidden.status_code == 200, hidden.text
    assert hidden.json()["library_hidden_columns"] == ["key", "date_added"]
    on_disk = json.loads((tmp_path / "data" / "state" / "ui-prefs.json").read_text(encoding="utf-8"))
    assert on_disk["library_hidden_columns"] == ["key", "date_added"]


@pytest.mark.requirement("LIBUX-13824")
def test_bitrate_and_kind_columns_are_hideable_over_http(prefs_client: TestClient, tmp_path: Path) -> None:
    """[if] bitrate and kind are PUT shown then hidden [then] both persist to disk in table order, [else stop]."""
    shown = prefs_client.put("/api/v1/ui-prefs", json={"library_hidden_columns": ["album"]})
    assert shown.status_code == 200, shown.text
    assert not {"bitrate", "kind"} & set(shown.json()["library_hidden_columns"])
    hidden = prefs_client.put("/api/v1/ui-prefs", json={"library_hidden_columns": ["kind", "key", "bitrate"]})
    assert hidden.status_code == 200, hidden.text
    assert hidden.json()["library_hidden_columns"] == ["key", "bitrate", "kind"]
    on_disk = json.loads((tmp_path / "data" / "state" / "ui-prefs.json").read_text(encoding="utf-8"))
    assert on_disk["library_hidden_columns"] == ["key", "bitrate", "kind"]


@pytest.mark.requirement("LIBUX-13845")
def test_last_played_column_is_hideable_over_http(prefs_client: TestClient, tmp_path: Path) -> None:
    """[if] last_played is PUT shown then hidden [then] both persist to disk in table order, [else stop]."""
    shown = prefs_client.put("/api/v1/ui-prefs", json={"library_hidden_columns": ["album"]})
    assert shown.status_code == 200, shown.text
    assert "last_played" not in shown.json()["library_hidden_columns"]
    hidden = prefs_client.put("/api/v1/ui-prefs", json={"library_hidden_columns": ["last_played", "key"]})
    assert hidden.status_code == 200, hidden.text
    assert hidden.json()["library_hidden_columns"] == ["key", "last_played"]
    on_disk = json.loads((tmp_path / "data" / "state" / "ui-prefs.json").read_text(encoding="utf-8"))
    assert on_disk["library_hidden_columns"] == ["key", "last_played"]


def test_hidden_columns_round_trip_to_disk_in_table_order(prefs_client: TestClient, tmp_path: Path) -> None:
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


@pytest.mark.requirement("LIBUX-10719")
def test_file_path_columns_round_trip_to_disk(prefs_client: TestClient, tmp_path: Path) -> None:
    """[if] filename is PUT hidden [then] it persists through GET and disk, [else stop]."""
    put = prefs_client.put("/api/v1/ui-prefs", json={"library_hidden_columns": ["filename", "tags"]})
    assert put.status_code == 200, put.text
    assert put.json()["library_hidden_columns"] == ["tags", "filename"]
    on_disk = json.loads((tmp_path / "data" / "state" / "ui-prefs.json").read_text(encoding="utf-8"))
    assert on_disk["library_hidden_columns"] == ["tags", "filename"]
    assert prefs_client.get("/api/v1/ui-prefs").json()["library_hidden_columns"] == ["tags", "filename"]


def test_tags_column_round_trips_to_disk(prefs_client: TestClient, tmp_path: Path) -> None:
    put = prefs_client.put("/api/v1/ui-prefs", json={"library_hidden_columns": ["tags"]})
    assert put.status_code == 200, put.text
    assert put.json()["library_hidden_columns"] == ["tags"]
    on_disk = json.loads(
        (tmp_path / "data" / "state" / "ui-prefs.json").read_text(encoding="utf-8")
    )
    assert on_disk["library_hidden_columns"] == ["tags"]
    assert prefs_client.get("/api/v1/ui-prefs").json()["library_hidden_columns"] == ["tags"]


@pytest.mark.parametrize(
    "bad",
    [["title"], ["lyrics"], ["key", "key"], "key", [1]],
    ids=["title-not-hideable", "lyrics-has-own-switch", "duplicate", "not-a-list", "not-a-string"],
)
def test_put_refuses_a_bad_hidden_column_list(prefs_client: TestClient, bad: object) -> None:
    put = prefs_client.put("/api/v1/ui-prefs", json={"library_hidden_columns": bad})
    assert put.status_code == 422, put.text
    assert prefs_client.get("/api/v1/ui-prefs").json()["library_hidden_columns"] == [
        "album",
        "filename",
        "location",
        "date_added",
        "bitrate",
        "kind",
        "last_played",
    ]


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
    browser = tuple(re.findall(r"'([a-z_]+)'", block.group(1)))
    assert "tags" in browser
    assert browser == _LIBRARY_HIDEABLE_COLUMNS
