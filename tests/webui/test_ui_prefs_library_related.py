"""LIBUX-10046: Related Tracks criteria live in ui-prefs as library_related_criteria.

[if] the browser or an agent reads or PUTs the criteria [then] ui-prefs.json holds a valid set, [else stop].

Regression lines:
- if GET /api/v1/ui-prefs lacks library_related_criteria then the cog has nothing to show
- if a partial PUT drops the other fields then changing one control resets the rest
- if bpm_percent 0, 21, true or 6.0 is stored then the related route gets a bad range
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

pytestmark = pytest.mark.requirement("LIBUX-10046")

DEFAULTS = {
    "bpm_percent": 6,
    "key": True,
    "genre": True,
    "color": False,
    "rating": False,
    "mytag": False,
    "artist": False,
}


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


def test_default_on_get(prefs_client: TestClient) -> None:
    """[if] GET lacks the #9753 defaults [then] the cog shows nothing, [else stop]."""
    assert prefs_client.get("/api/v1/ui-prefs").json()["library_related_criteria"] == DEFAULTS


def test_partial_put_merges_and_persists(prefs_client: TestClient, tmp_path: Path) -> None:
    """[if] a partial PUT drops other fields or is not on disk [then] one control resets the rest, [else stop]."""
    assert prefs_client.put("/api/v1/ui-prefs", json={"library_related_criteria": {"color": True}}).status_code == 200
    r = prefs_client.put("/api/v1/ui-prefs", json={"library_related_criteria": {"bpm_percent": 4}})
    assert r.status_code == 200, r.text
    expected = {**DEFAULTS, "color": True, "bpm_percent": 4}
    assert r.json()["library_related_criteria"] == expected
    on_disk = json.loads((tmp_path / "data" / "state" / "ui-prefs.json").read_text())
    assert on_disk["library_related_criteria"] == expected


@pytest.mark.parametrize(
    "value",
    [{"bpm_percent": 0}, {"bpm_percent": 21}, {"bpm_percent": True}, {"bpm_percent": 6.0}, {"key": 1}, {"bogus": True}],
)
def test_invalid_values_rejected(prefs_client: TestClient, value: dict) -> None:
    """[if] a bad criteria value is stored [then] the related route gets nonsense, [else stop]."""
    r = prefs_client.put("/api/v1/ui-prefs", json={"library_related_criteria": value})
    assert r.status_code == 422, r.text
    assert prefs_client.get("/api/v1/ui-prefs").json()["library_related_criteria"] == DEFAULTS


def test_malformed_on_disk_is_422(prefs_client: TestClient, tmp_path: Path) -> None:
    """[if] a malformed stored value loads [then] GET coerces instead of refusing, [else stop]."""
    path = tmp_path / "data" / "state" / "ui-prefs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"library_related_criteria": {"bpm_percent": 99}}))
    r = prefs_client.get("/api/v1/ui-prefs")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "UI_PREFS_INVALID"
