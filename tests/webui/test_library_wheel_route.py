"""LIBUX-06 route: real genre-grouped library over HTTP, agent-parity CLI.

Requirements:
✔︎ GET /api/v1/library/wheel returns the real genre tree, never demo nodes.
✔︎ GET /api/v1/library/wheel/axes exposes enabled/disabled + reason for a picker.
✔︎ A disabled axis (?axis=decade) returns 200 with axis_value null everywhere, not a 500/fake number.

Acceptance tests:
[if] the daemon has a real state.db + master.plain.db [then ⛔️] the wheel route returns demo data
[if] axis=decade|overplayed_ness|set_played_in is requested [then ⛔️] any axis_value is non-null
[if] state.db is unavailable [then ⛔️] the route returns 200 with an invented tree
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared import paths as shared_paths
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend
from tests.webui.library_wheel_fixtures import _make_master_db, _make_state_db


@pytest.fixture
def wheel_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    state_db = tmp_path / "state.db"
    master_db = tmp_path / "master.plain.db"
    _make_state_db(
        state_db,
        tracks=[{"stable_id": "t1", "title": "Warehouse", "artists": ["Artist A"]}],
        memberships=[],
        playlists=[],
        vendor_ids={"t1": "v1"},
    )
    _make_master_db(
        master_db, content=[{"vendor_id": "v1", "genre_id": "g-techno", "play_count": 7}]
    )
    monkeypatch.setattr(shared_paths, "STATE_DB", state_db)
    monkeypatch.setattr(shared_paths, "REKORDBOX_PLAIN_DB", master_db)

    app = create_app(
        backend=SqliteBackend(state_db), bind_host="127.0.0.1", hostname="test-host",
        lock_status_fn=lambda: None, syncthing_status_fn=lambda: None,
    )
    with TestClient(app) as c:
        yield c


def test_wheel_returns_real_genre_tree(wheel_client: TestClient) -> None:
    resp = wheel_client.get("/api/v1/library/wheel", params={"axis": "play_count"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_tracks"] == 1
    assert body["families"][0]["name"] == "techno"
    track = body["families"][0]["genres"][0]["tracks"][0]
    assert track["stable_id"] == "t1"
    assert track["axis_value"] == 7


def test_wheel_disabled_axis_returns_200_with_null_values_not_fabricated(
    wheel_client: TestClient,
) -> None:
    resp = wheel_client.get("/api/v1/library/wheel", params={"axis": "decade"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["selected_axis_enabled"] is False
    assert body["selected_axis_reason"]
    track = body["families"][0]["genres"][0]["tracks"][0]
    assert track["axis_value"] is None


def test_wheel_unknown_axis_is_422(wheel_client: TestClient) -> None:
    resp = wheel_client.get("/api/v1/library/wheel", params={"axis": "not-a-real-axis"})
    assert resp.status_code == 422


def test_wheel_axes_endpoint_names_all_seven(wheel_client: TestClient) -> None:
    resp = wheel_client.get("/api/v1/library/wheel/axes")
    assert resp.status_code == 200
    keys = {a["key"] for a in resp.json()}
    assert keys == {
        "genre", "decade", "play_count", "popularity",
        "overplayed_ness", "playlist", "set_played_in",
    }


def test_wheel_missing_state_db_is_503_not_200(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shared_paths, "STATE_DB", tmp_path / "missing-state.db")
    monkeypatch.setattr(shared_paths, "REKORDBOX_PLAIN_DB", tmp_path / "missing-master.db")
    app = create_app(
        backend=SqliteBackend(tmp_path / "missing-state.db"),
        bind_host="127.0.0.1", hostname="test-host",
        lock_status_fn=lambda: None, syncthing_status_fn=lambda: None,
    )
    with TestClient(app) as c:
        resp = c.get("/api/v1/library/wheel")
    assert resp.status_code == 503
