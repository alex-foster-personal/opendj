"""Cue-marker visibility prefs over HTTP (issue #10248, WAVEQS-11730).

[if] a cue-marker pref is PUT [then] it persists, defaults on, [else stop].

Regression lines:
- if any show_*_cue_markers key is absent from GET defaults then overlays boot wrong
- if a PUT does not land in ui-prefs.json then the control is decorative
- if a non-boolean is coerced rather than refused then a typo silently hides markers
- if a PUT of one key resets the others then the toggles fight
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.sqlite_backend import SqliteBackend

pytestmark = pytest.mark.requirement("WAVEQS-11730")

_KEYS = ("show_hot_cue_markers", "show_memory_cue_markers", "show_loop_markers")


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


def _prefs_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "state" / "ui-prefs.json"


def _disk(tmp_path: Path) -> dict[str, object]:
    return json.loads(_prefs_path(tmp_path).read_text())


def test_cue_marker_defaults_all_on(prefs_client: TestClient) -> None:
    """[if] ui-prefs are fetched with nothing stored [then] all three markers default on, [else stop]."""
    r = prefs_client.get("/api/v1/ui-prefs")
    assert r.status_code == 200
    body = r.json()
    for key in _KEYS:
        assert body[key] is True


@pytest.mark.parametrize("key", _KEYS)
def test_cue_marker_put_round_trips(prefs_client: TestClient, tmp_path: Path, key: str) -> None:
    """[if] a PUT toggles one marker key [then] reply, disk and GET carry it, [else stop]."""
    r = prefs_client.put("/api/v1/ui-prefs", json={key: False})
    assert r.status_code == 200
    assert r.json()[key] is False
    assert _disk(tmp_path)[key] is False
    got = prefs_client.get("/api/v1/ui-prefs").json()
    assert got[key] is False
    r = prefs_client.put("/api/v1/ui-prefs", json={key: True})
    assert r.json()[key] is True


def test_cue_marker_put_of_one_key_leaves_the_others(prefs_client: TestClient) -> None:
    """[if] a PUT names one cue-marker key [then] the other two keep theirs, [else stop]."""
    prefs_client.put("/api/v1/ui-prefs", json={"show_hot_cue_markers": False, "show_sections": True})
    r = prefs_client.put("/api/v1/ui-prefs", json={"show_memory_cue_markers": False})
    assert r.json()["show_hot_cue_markers"] is False
    assert r.json()["show_loop_markers"] is True
    assert r.json()["show_sections"] is True


@pytest.mark.parametrize("key", _KEYS)
@pytest.mark.parametrize("bad", ["yes", 1, 0, "false"])
def test_cue_marker_put_rejects_non_boolean(prefs_client: TestClient, tmp_path: Path, key: str, bad: object) -> None:
    """[if] a PUT carries a non-boolean [then] 422 and disk unchanged, [else stop]."""
    r = prefs_client.put("/api/v1/ui-prefs", json={key: bad})
    assert r.status_code == 422, r.text
    assert not _prefs_path(tmp_path).exists()
