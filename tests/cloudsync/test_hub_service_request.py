"""Hub request wiring: shared ``_db_path`` / ``_data_dir`` (issue #5304).

[if] ``state_db_path`` is unset on a hub route [then] 503 SYNC_NO_DB, [else stop].

Every test drives the real hub router through a real Starlette request, and
the path tests sync a real spoke against it: the helpers are proven by where
the hub's own writes land, never by calling them on a stand-in request. All
paths derive from ``tmp_path`` so they are native on every OS.

Nothing here touches the process environment. ``MDT_IS_HUB`` decides only
which role each side registers with, never where the hub's writes land, so
these tests hold in the unmodified process context whatever it is: unset or
``0`` (a spoke's shell, CI) and ``1`` (a hub's shell) alike. An illegible
value fails the sync loudly, as it would in production.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.shared.state import machine_identity
from apps.sync_hub import client, service
from apps.sync_hub.service_request import (
    SYNC_NO_DB_MESSAGE_ASSET_ROUTERS,
    SYNC_NO_DB_MESSAGE_MAIN,
)
from tests.cloudsync.enrollment_transport import TestClientTransport


def _bare_hub_app() -> FastAPI:
    app = FastAPI()
    app.include_router(service.router, prefix="/api/v1")
    return app


def _assert_sync_no_db(response, *, message: str) -> None:
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail == {"code": "SYNC_NO_DB", "message": message}


@pytest.mark.requirement("CAT-04")
def test_status_returns_sync_no_db_without_state_db_path() -> None:
    """[if] GET /status runs without state_db_path [then] SYNC_NO_DB, [else stop]."""
    client = TestClient(_bare_hub_app())
    _assert_sync_no_db(
        client.get("/api/v1/sync/status", params={"machine_id": "spoke-a"}),
        message=SYNC_NO_DB_MESSAGE_MAIN,
    )


@pytest.mark.requirement("STEM-31")
def test_stems_index_returns_sync_no_db_without_state_db_path() -> None:
    """[if] GET /stems/index runs without state_db_path [then] SYNC_NO_DB, [else stop]."""
    client = TestClient(_bare_hub_app())
    _assert_sync_no_db(
        client.get("/api/v1/sync/stems/index", params={"machine_id": "spoke-a"}),
        message=SYNC_NO_DB_MESSAGE_ASSET_ROUTERS,
    )


@pytest.mark.requirement("LYRICS-07")
def test_lyrics_asr_returns_sync_no_db_without_state_db_path() -> None:
    """[if] GET /lyrics-asr/* runs without state_db_path [then] SYNC_NO_DB, [else stop]."""
    client = TestClient(_bare_hub_app())
    _assert_sync_no_db(
        client.get("/api/v1/sync/lyrics-asr/trk-1", params={"machine_id": "spoke-a"}),
        message=SYNC_NO_DB_MESSAGE_ASSET_ROUTERS,
    )


def _hub_app(state: dict[str, str]) -> FastAPI:
    app = _bare_hub_app()
    for name, value in state.items():
        setattr(app.state, name, value)
    return app


def _sync_spoke(app: FastAPI, spoke_dir: Path) -> str:
    """One real sync from a fresh spoke through the real router; the hub's id."""
    with TestClient(app) as http:
        result = client.run_sync(spoke_dir, "http://hub.invalid", transport=TestClientTransport(http), name="spoke-a")
    return result.hub_machine_id


def _hub_rows(db: Path) -> list[str]:
    conn = state_db.open_ro(db)
    try:
        return [row[0] for row in conn.execute("SELECT machine_id FROM machines ORDER BY machine_id")]
    finally:
        conn.close()


def test_hub_routes_open_the_configured_state_db(tmp_path: Path) -> None:
    """[if] state_db_path is set [then] every hub write lands in that file, [else stop]."""
    db = tmp_path / "hub" / "state" / "state.db"
    assert not db.exists()
    hub_id = _sync_spoke(
        _hub_app({"state_db_path": str(db), "sync_hub_data_dir": str(tmp_path / "hub")}),
        tmp_path / "spoke-a",
    )
    assert hub_id in _hub_rows(db), "the hub registered itself somewhere other than state_db_path"


def test_hub_identity_lives_in_sync_hub_data_dir_when_set(tmp_path: Path) -> None:
    """[if] sync_hub_data_dir is set [then] the hub's machine-id is minted there, [else stop]."""
    db = tmp_path / "db-root" / "state" / "state.db"
    data_dir = tmp_path / "hub-data"
    hub_id = _sync_spoke(
        _hub_app({"state_db_path": str(db), "sync_hub_data_dir": str(data_dir)}),
        tmp_path / "spoke-a",
    )
    assert machine_identity.machine_id_path(data_dir).read_text(encoding="utf-8").strip() == hub_id
    # Control: the DB-derived root, which the explicit setting must override, holds no id.
    assert not machine_identity.machine_id_path(db.parent.parent).exists()


def test_hub_identity_derives_from_state_db_path_without_data_dir(tmp_path: Path) -> None:
    """[if] sync_hub_data_dir is unset [then] machine-id sits at <data-dir> of <data-dir>/state/state.db, [else stop]."""
    hub_root = tmp_path / "hub"
    db = hub_root / "state" / "state.db"
    hub_id = _sync_spoke(_hub_app({"state_db_path": str(db)}), tmp_path / "spoke-a")
    assert machine_identity.machine_id_path(hub_root).read_text(encoding="utf-8").strip() == hub_id
