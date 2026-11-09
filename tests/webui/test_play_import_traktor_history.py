"""Agent parity for the Traktor history import (DJIMPORT-10069).

[if] an agent POSTs a Traktor folder to /api/v1/plays/import/traktor-history [then] its played entries land in the play log once, [else stop].

Regression one-liners:
  - if the route does not append plays then agents cannot import history without the CLI
  - if a repeated POST appends again then the play count inflates
  - if a missing History/ or a bad zone answers 200 then errors are silent
"""
from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.webui.server.routes.play_import import router
from apps.webui.server.sqlite_backend import make_backend

pytestmark = [pytest.mark.requirement("DJIMPORT-10069")]

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "traktor_nml" / "history_minimal.nml"
URL = "/api/v1/plays/import/traktor-history"


@pytest.fixture
def env(tmp_path: Path) -> Iterator[SimpleNamespace]:
    traktor = tmp_path / "Traktor 3.11"
    (traktor / "History").mkdir(parents=True)
    shutil.copy(FIXTURE, traktor / "History" / "history_2019y12m11d_17h00m00s.nml")
    state = tmp_path / "state.db"
    conn = state_db.open_rw(state)
    try:
        for name in ("a", "b", "c"):
            conn.execute(
                "INSERT INTO tracks (stable_id, stable_id_tier, title, file_path, created_at, updated_at) "
                "VALUES (?, 'inferred', ?, ?, '2026-01-01', '2026-01-01')",
                (name * 40, name, f"/Music/Set/{name}.mp3"),
            )
        conn.commit()
    finally:
        conn.close()
    app = FastAPI()
    app.state.backend = make_backend()
    app.state.state_db_path = state
    app.include_router(router, prefix="/api/v1")
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, traktor=traktor, tmp=tmp_path)


def test_post_imports_once(env: SimpleNamespace) -> None:
    body = {"source": str(env.traktor), "zone": "America/New_York", "dry_run": False}
    first = env.client.post(URL, json=body)
    assert first.status_code == 200, first.text
    assert first.json()["plays_added"] == 2
    assert first.json()["entries_not_public"] == 1
    again = env.client.post(URL, json=body).json()
    assert (again["plays_added"], again["plays_already_logged"]) == (0, 2)


def test_missing_history_is_404(env: SimpleNamespace) -> None:
    res = env.client.post(URL, json={"source": str(env.tmp / "nowhere"), "dry_run": True})
    assert res.status_code == 404
    assert res.json()["detail"]["code"] == "TRAKTOR_HISTORY_NOT_FOUND"


def test_bad_zone_is_422(env: SimpleNamespace) -> None:
    res = env.client.post(URL, json={"source": str(env.traktor), "zone": "Not/AZone", "dry_run": True})
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "BAD_ZONE"
