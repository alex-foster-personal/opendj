"""POST /api/v1/library/maintenance/optimize runs SQLite optimize only when confirmed (LIBM-126, issue #12332).

[if] the optimize route runs unconfirmed, on a missing DB, or hides its result [then] fail, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.shared.state import db_optimize
from apps.webui.server.routes import library_maintenance

pytestmark = pytest.mark.requirement("LIBM-14744")

URL = "/api/v1/library/maintenance/optimize"


@pytest.fixture
def library_dir(tmp_path: Path) -> Path:
    """A temp data dir with an initialized state.db (fixture only, never the real library)."""
    data_dir = tmp_path / "data"
    conn = state_db.open_rw(db_optimize.library_db_path(data_dir))
    conn.close()
    return data_dir


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(library_maintenance.router, prefix="/api/v1")
    return TestClient(app)


def test_unconfirmed_optimize_is_refused_and_runs_nothing(
    client: TestClient, library_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] optimize is posted with confirm=false [then] 400 CONFIRM_REQUIRED and the core is never called, [else stop]."""
    calls: list[Path] = []
    monkeypatch.setattr(db_optimize, "optimize_library_db", calls.append)
    response = client.post(URL, json={"data_dir": str(library_dir), "confirm": False})
    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "CONFIRM_REQUIRED"
    assert "VACUUM" in detail["message"]
    assert calls == []


def test_confirmed_optimize_reports_completion(client: TestClient, library_dir: Path) -> None:
    """[if] optimize is confirmed for an initialized library [then] 200 completed with path, sizes and time, [else stop]."""
    response = client.post(URL, json={"data_dir": str(library_dir), "confirm": True})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed"
    assert body["db_path"] == str(db_optimize.library_db_path(library_dir))
    assert body["bytes_before"] > 0 and body["bytes_after"] > 0
    assert body["elapsed_ms"] >= 0


def test_missing_library_db_is_404_and_creates_no_file(client: TestClient, tmp_path: Path) -> None:
    """[if] optimize targets a data dir with no state.db [then] 404 LIBRARY_DB_MISSING and no file appears, [else stop]."""
    empty = tmp_path / "empty"
    response = client.post(URL, json={"data_dir": str(empty), "confirm": True})
    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] == "LIBRARY_DB_MISSING"
    assert not db_optimize.library_db_path(empty).exists()


def test_db_without_library_schema_is_409(client: TestClient, tmp_path: Path) -> None:
    """[if] state.db exists but has no tracks table [then] 409 NOT_A_LIBRARY, [else stop]."""
    data_dir = tmp_path / "bare"
    conn = state_db.open_rw(db_optimize.library_db_path(data_dir), apply_schema=False)
    conn.execute("CREATE TABLE other (x INTEGER)")
    conn.close()
    response = client.post(URL, json={"data_dir": str(data_dir), "confirm": True})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "NOT_A_LIBRARY"
