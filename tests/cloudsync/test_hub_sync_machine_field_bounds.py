"""Issue #3081: sync hub machine.name and machine_id wire bounds.

[if] a hello carries a machine name or id over the wire limit [then] the hub
rejects it and no later response grows, [else stop].
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.shared.state import schema as state_schema
from apps.sync_hub import client, service
from apps.sync_hub.machine_wire_limits import (
    MACHINE_ID_MAX_LENGTH,
    MACHINE_NAME_MAX_LENGTH,
)
from tests.cloudsync.test_hub_sync import _T0

pytestmark = pytest.mark.requirement("CLOUDSYNC-21")

_HELLO = "/api/v1/sync/hello"
_PUSH = "/api/v1/sync/push"
_PULL = "/api/v1/sync/pull"
_STATUS = "/api/v1/sync/status"

_ADV_ID = "adv6-huge-1"
_SPOKE_A = "cccccccccccccccccccccccccccccccc"
_SPOKE_B = "dddddddddddddddddddddddddddddddd"


@pytest.fixture(autouse=True)
def _no_hub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MDT_IS_HUB", raising=False)


@pytest.fixture
def hub_dir(tmp_path: Path) -> Path:
    return tmp_path / "hub"


@pytest.fixture
def hub_client(hub_dir: Path) -> Iterator[TestClient]:
    app = FastAPI()
    app.state.state_db_path = str(client.state_db_path(hub_dir))
    app.state.sync_hub_data_dir = str(hub_dir)
    app.state.sync_hub_machine_name = "hub"
    app.include_router(service.router, prefix="/api/v1")
    with TestClient(app) as http:
        yield http


def _machine(
    *,
    machine_id: str,
    name: str,
    platform: str = "macos",
) -> dict[str, Any]:
    return {
        "machine_id": machine_id,
        "name": name,
        "platform": platform,
        "is_hub": False,
        "data_root": None,
        "first_seen": _T0,
        "last_seen": _T0,
    }


def _hello_body(
    machine: dict[str, Any], *, machines: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "machine": machine,
        "schema_version": state_schema.SCHEMA_VERSION,
        "machines": machines or [],
    }


def _hub_conn(hub_dir: Path) -> sqlite3.Connection:
    return state_db.open_rw(client.state_db_path(hub_dir))


def _machine_ids(conn: sqlite3.Connection) -> list[str]:
    return [str(row[0]) for row in conn.execute("SELECT machine_id FROM machines")]


def test_hello_refuses_oversize_name_and_stores_nothing(
    hub_client: TestClient, hub_dir: Path
) -> None:
    """[if] hello name exceeds 256 chars [then] hub 422s and stores nothing, [else stop]."""
    response = hub_client.post(
        _HELLO,
        json=_hello_body(_machine(machine_id=_ADV_ID, name="x" * 200_000)),
    )
    assert response.status_code == 422
    conn = _hub_conn(hub_dir)
    try:
        assert _ADV_ID not in _machine_ids(conn)
    finally:
        conn.close()


def test_hello_refuses_oversize_machine_id(hub_client: TestClient, hub_dir: Path) -> None:
    """[if] hello machine_id exceeds 256 chars [then] hub 422s, [else stop]."""
    machine_id = "a" * 300
    response = hub_client.post(
        _HELLO,
        json=_hello_body(_machine(machine_id=machine_id, name="spoke")),
    )
    assert response.status_code == 422
    conn = _hub_conn(hub_dir)
    try:
        assert machine_id not in _machine_ids(conn)
    finally:
        conn.close()


def test_hello_refuses_oversize_peer_in_machines_array(
    hub_client: TestClient, hub_dir: Path
) -> None:
    """[if] machines[] carries oversize peer name [then] hub 422s before merge, [else stop]."""
    caller = _machine(machine_id=_SPOKE_A, name="caller")
    peer = _machine(machine_id=_SPOKE_B, name="p" * 300)
    response = hub_client.post(
        _HELLO,
        json=_hello_body(caller, machines=[peer]),
    )
    assert response.status_code == 422
    conn = _hub_conn(hub_dir)
    try:
        assert _SPOKE_B not in _machine_ids(conn)
    finally:
        conn.close()


def test_push_refuses_oversize_fleet_snapshot(hub_client: TestClient) -> None:
    """[if] push machines[] carries oversize peer name [then] hub 422s, [else stop]."""
    hub_client.post(
        _HELLO,
        json=_hello_body(_machine(machine_id=_SPOKE_A, name="spoke-a")),
    ).raise_for_status()
    response = hub_client.post(
        _PUSH,
        json={
            "machine_id": _SPOKE_A,
            "schema_version": state_schema.SCHEMA_VERSION,
            "rows": [],
            "machines": [_machine(machine_id=_SPOKE_B, name="p" * 300)],
        },
    )
    assert response.status_code == 422


def test_legacy_oversized_name_clamped_in_hello_pull_status(
    hub_client: TestClient, hub_dir: Path
) -> None:
    """[if] sqlite holds legacy oversize name [then] responses clamp it, [else stop]."""
    conn = _hub_conn(hub_dir)
    try:
        conn.execute(
            "INSERT INTO machines(machine_id, name, platform, is_hub, data_root, "
            "first_seen, last_seen) VALUES (?, ?, 'linux', 0, NULL, ?, ?)",
            ("legacy-poison-id", "p" * 200_000, _T0, _T0),
        )
        conn.commit()
    finally:
        conn.close()

    hello = hub_client.post(
        _HELLO,
        json=_hello_body(_machine(machine_id=_SPOKE_A, name="fresh-spoke")),
    )
    assert hello.status_code == 200
    hello_json = hello.json()
    assert len(json.dumps(hello_json)) < 50_000
    for row in hello_json["machines"]:
        assert len(row["name"]) <= MACHINE_NAME_MAX_LENGTH
        assert len(row["machine_id"]) <= MACHINE_ID_MAX_LENGTH

    pull = hub_client.get(_PULL, params={"machine_id": _SPOKE_A})
    assert pull.status_code == 200
    for row in pull.json()["machines"]:
        assert len(row["name"]) <= MACHINE_NAME_MAX_LENGTH

    status = hub_client.get(_STATUS, params={"machine_id": _SPOKE_A})
    assert status.status_code == 200
    for row in status.json()["machines"]:
        assert len(row["name"]) <= MACHINE_NAME_MAX_LENGTH


def test_at_limit_machine_fields_round_trip(hub_client: TestClient) -> None:
    """[if] hello name is exactly 256 chars [then] hub accepts and returns it, [else stop]."""
    machine_id = "a" * 32
    name = "n" * MACHINE_NAME_MAX_LENGTH
    response = hub_client.post(
        _HELLO,
        json=_hello_body(_machine(machine_id=machine_id, name=name)),
    )
    assert response.status_code == 200
    names = {row["name"] for row in response.json()["machines"] if row["machine_id"] == machine_id}
    assert names == {name}
