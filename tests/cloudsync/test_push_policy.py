"""Issue #13162: sync push policy validation boundaries beyond cache_budget.

CLOUDSYNC-21 wired :func:`apps.sync_hub.policy_push.evaluate_push_policies` on
``POST /api/v1/sync/push``. Round 6 covered ``cache_budget`` only; this module
proves ``pin_target`` on the wire path and that blocked batches never reach
:func:`apps.sync_hub.engine.hub_apply`.

- [if] invalid push policy reaches hub_apply [then] pre-transport gate failed, [else stop].
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.state import schema as state_schema
from apps.sync_hub import client, engine, service, wire_version

pytestmark = pytest.mark.requirement("CAT-04")

_SPOKE = "spoke-x"
_STAMP = "2026-09-15T00:00:00.000000+00:00"
_PL_LIVE = "pl-live"
_PL_ABSENT = "pl-absent"


@pytest.fixture(autouse=True)
def _no_hub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MDT_IS_HUB", raising=False)


@pytest.fixture
def hub_dir(tmp_path: Path) -> Path:
    return tmp_path / "hub"


@pytest.fixture
def hub_app(hub_dir: Path) -> FastAPI:
    app = FastAPI()
    app.state.state_db_path = str(client.state_db_path(hub_dir))
    app.state.sync_hub_data_dir = str(hub_dir)
    app.state.sync_hub_machine_name = "hub"
    app.include_router(service.router, prefix="/api/v1")
    return app


@pytest.fixture
def http(hub_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(hub_app) as client_http:
        yield client_http


def _hello(http: TestClient) -> None:
    response = http.post(
        "/api/v1/sync/hello",
        json={
            "machine": {
                "machine_id": _SPOKE,
                "name": "host-x",
                "platform": "macos",
                "is_hub": False,
                "data_root": None,
                "first_seen": _STAMP,
                "last_seen": _STAMP,
            },
            "schema_version": state_schema.SCHEMA_VERSION,
            "wire_version": wire_version.WIRE_VERSION,
            "capabilities": ["quarantine/v1"],
        },
    )
    assert response.status_code == 200


def _push_rows(http: TestClient, rows: list[dict[str, Any]]) -> Any:
    return http.post(
        "/api/v1/sync/push",
        json={
            "machine_id": _SPOKE,
            "schema_version": state_schema.SCHEMA_VERSION,
            "wire_version": wire_version.WIRE_VERSION,
            "capabilities": ["quarantine/v1"],
            "rows": rows,
        },
    )


def _hub_count(hub_dir: Path, sql: str, params: tuple[str, ...] = ()) -> int:
    conn = sqlite3.connect(client.state_db_path(hub_dir))
    try:
        row = conn.execute(sql, params).fetchone()
        return int(row[0]) if row is not None else 0
    finally:
        conn.close()


def _live_playlist_row(playlist_id: str) -> dict[str, Any]:
    return {
        "table": "playlists",
        "pk": [playlist_id],
        "values": {
            "playlist_id": playlist_id,
            "name": "Live playlist",
            "vendor": "open-dj",
            "vendor_pl_id": playlist_id,
            "created_at": _STAMP,
            "updated_at": _STAMP,
            "origin_device_id": _SPOKE,
            "deleted_at": None,
            "forbid_duplicates": 0,
            "parent_id": None,
            "kind": "playlist",
            "stage": None,
            "set_status": None,
            "sort_index": None,
        },
        "members": [],
    }


def _pin_row(playlist_id: str) -> dict[str, Any]:
    return {
        "table": "playlist_pins",
        "pk": [_SPOKE, playlist_id],
        "values": {
            "machine_id": _SPOKE,
            "playlist_id": playlist_id,
            "mode": "pinned",
            "updated_at": _STAMP,
            "origin_device_id": _SPOKE,
            "deleted_at": None,
        },
    }


def test_push_pin_on_co_shipped_live_playlist_is_accepted(http: TestClient, hub_dir: Path) -> None:
    """if pin on co-shipped live playlist is refused then allowed target broke."""
    _hello(http)
    response = _push_rows(http, [_live_playlist_row(_PL_LIVE), _pin_row(_PL_LIVE)])
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] >= 1
    assert body.get("detail") is None
    assert (
        _hub_count(
            hub_dir,
            "SELECT COUNT(*) FROM playlist_pins WHERE machine_id = ? AND playlist_id = ?",
            (_SPOKE, _PL_LIVE),
        )
        == 1
    )


def test_push_pin_on_missing_playlist_is_422_and_writes_nothing(http: TestClient, hub_dir: Path) -> None:
    """if pin on absent playlist is accepted then unknown target gate failed."""
    _hello(http)
    changelog_before = _hub_count(hub_dir, "SELECT COUNT(*) FROM hub_changelog")
    pins_before = _hub_count(
        hub_dir,
        "SELECT COUNT(*) FROM playlist_pins WHERE machine_id = ?",
        (_SPOKE,),
    )
    response = _push_rows(http, [_pin_row(_PL_ABSENT)])
    assert response.status_code == 422
    body = response.json()
    assert body["detail"]["code"] == "SYNC_POLICY_VIOLATION"
    violations = body["detail"]["violations"]
    assert any(v["rule_id"] == "pin_target" for v in violations)
    assert _hub_count(hub_dir, "SELECT COUNT(*) FROM hub_changelog") == changelog_before
    assert (
        _hub_count(
            hub_dir,
            "SELECT COUNT(*) FROM playlist_pins WHERE machine_id = ?",
            (_SPOKE,),
        )
        == pins_before
    )


def test_push_policy_violation_does_not_call_hub_apply(http: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """if hub_apply runs on policy block then validation is not pre-transport."""
    _hello(http)
    calls: list[tuple[Any, ...]] = []

    def _forbidden_hub_apply(conn: sqlite3.Connection, changes: Any) -> Any:
        calls.append((conn, changes))
        pytest.fail("hub_apply must not run when policy blocks")

    monkeypatch.setattr(engine, "hub_apply", _forbidden_hub_apply)
    response = _push_rows(http, [_pin_row(_PL_ABSENT)])
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "SYNC_POLICY_VIOLATION"
    assert calls == []


def test_push_unknown_asset_kind_is_422_and_writes_nothing(http: TestClient, hub_dir: Path) -> None:
    """if unknown asset_kind push is accepted then vocabulary gate failed."""
    _hello(http)
    response = _push_rows(
        http,
        [
            {
                "table": "sync_policies",
                "pk": [_SPOKE, "video"],
                "values": {
                    "machine_id": _SPOKE,
                    "asset_kind": "video",
                    "mode": "pinned",
                    "cache_budget_mb": None,
                    "updated_at": _STAMP,
                    "origin_device_id": _SPOKE,
                    "deleted_at": None,
                },
            }
        ],
    )
    assert response.status_code == 422
    body = response.json()
    assert body["detail"]["code"] == "SYNC_POLICY_VIOLATION"
    assert any(v["rule_id"] == "unknown_asset_kind" for v in body["detail"]["violations"])
    assert (
        _hub_count(
            hub_dir,
            "SELECT COUNT(*) FROM sync_policies WHERE machine_id = ? AND asset_kind = 'video'",
            (_SPOKE,),
        )
        == 0
    )
