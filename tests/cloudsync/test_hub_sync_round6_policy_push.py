"""Round 6 adversarial: sync/push must enforce cache-budget policy invariants.

Issue #3080: ``POST /api/v1/sync/push`` wrote ``sync_policies`` rows through the
generic LWW path without running :func:`apps.sync_hub.policy_store.evaluate`,
so invalid ``cache_budget_mb`` values landed in ``hub_changelog`` and propagated
to every peer on pull.

Acceptance criteria, one test each:
- if a push offers ``cache_budget_mb <= 0`` the hub 422s naming ``cache_budget``
- if a push offers a budget while ``mode != cached`` the hub 422s naming it
- if a valid cached budget push is refused, legitimate sync is broken
- if pull returns a row a refused push offered, the gate did not roll back

- [if] push offers invalid cache_budget [then] hub 422s SYNC_POLICY_VIOLATION, [else stop].
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
from apps.sync_hub import client, service, wire_version

pytestmark = pytest.mark.requirement("CAT-04")

_SPOKE = "spoke-x"
_STAMP = "2026-09-15T00:00:00.000000+00:00"


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


def _push_policy(http: TestClient, *, mode: str, budget: int | None) -> Any:
    return http.post(
        "/api/v1/sync/push",
        json={
            "machine_id": _SPOKE,
            "schema_version": state_schema.SCHEMA_VERSION,
            "wire_version": wire_version.WIRE_VERSION,
            "capabilities": ["quarantine/v1"],
            "rows": [
                {
                    "table": "sync_policies",
                    "pk": [_SPOKE, "audio"],
                    "values": {
                        "machine_id": _SPOKE,
                        "asset_kind": "audio",
                        "mode": mode,
                        "cache_budget_mb": budget,
                        "updated_at": _STAMP,
                        "origin_device_id": _SPOKE,
                        "deleted_at": None,
                    },
                }
            ],
        },
    )


def _hub_count(hub_dir: Path, sql: str, params: tuple[str, ...] = ()) -> int:
    conn = sqlite3.connect(client.state_db_path(hub_dir))
    try:
        row = conn.execute(sql, params).fetchone()
        return int(row[0]) if row is not None else 0
    finally:
        conn.close()


def test_push_negative_cache_budget_is_422_and_writes_nothing(
    http: TestClient, hub_dir: Path
) -> None:
    """if push offers cache_budget_mb <= 0 then hub 422s cache_budget, else stop."""
    _hello(http)
    changelog_before = _hub_count(hub_dir, "SELECT COUNT(*) FROM hub_changelog")
    response = _push_policy(http, mode="cached", budget=-42)
    assert response.status_code == 422
    body = response.json()
    assert body["detail"]["code"] == "SYNC_POLICY_VIOLATION"
    assert any(v["rule_id"] == "cache_budget" for v in body["detail"]["violations"])
    assert _hub_count(
        hub_dir,
        "SELECT COUNT(*) FROM sync_policies WHERE machine_id = ? AND asset_kind = 'audio'",
        (_SPOKE,),
    ) == 0
    assert _hub_count(hub_dir, "SELECT COUNT(*) FROM hub_changelog") == changelog_before


def test_push_budget_on_non_cached_mode_is_422_and_writes_nothing(
    http: TestClient, hub_dir: Path
) -> None:
    """if push offers budget while mode != cached then hub 422s cache_budget, else stop."""
    _hello(http)
    response = _push_policy(http, mode="stream", budget=512)
    assert response.status_code == 422
    body = response.json()
    assert body["detail"]["code"] == "SYNC_POLICY_VIOLATION"
    assert any(v["rule_id"] == "cache_budget" for v in body["detail"]["violations"])
    assert _hub_count(
        hub_dir,
        "SELECT COUNT(*) FROM sync_policies WHERE machine_id = ? AND asset_kind = 'audio'",
        (_SPOKE,),
    ) == 0


def test_push_valid_cached_budget_is_accepted(http: TestClient, hub_dir: Path) -> None:
    """if a legal cached row with positive budget is refused then sync is broken."""
    _hello(http)
    response = _push_policy(http, mode="cached", budget=512)
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] == 1
    assert body["rejected"] == 0
    conn = sqlite3.connect(client.state_db_path(hub_dir))
    try:
        budget = conn.execute(
            "SELECT cache_budget_mb FROM sync_policies "
            "WHERE machine_id = ? AND asset_kind = 'audio'",
            (_SPOKE,),
        ).fetchone()
    finally:
        conn.close()
    assert budget == (512,)


def test_pull_does_not_return_rejected_policy_row(http: TestClient, hub_dir: Path) -> None:
    """if pull returns a row a refused push offered then the gate did not roll back."""
    _hello(http)
    assert _push_policy(http, mode="cached", budget=-42).status_code == 422
    pull = http.get(
        "/api/v1/sync/pull",
        params={"machine_id": _SPOKE, "since_seq": 0},
    )
    assert pull.status_code == 200
    rows = pull.json()["rows"]
    assert not any(
        row.get("table") == "sync_policies"
        and row.get("pk") == [_SPOKE, "audio"]
        for row in rows
    )
