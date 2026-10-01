"""One machine, one name: every sync entry point registers this data dir under
the CONFIGURED CloudSync machine name, never the bare OS hostname.

Regression (Thu 1 Oct 2026): a freshly enrolled second data dir on a host had
pulled the fleet's ``machines`` table, so the host's name already belonged to
another machine id. The feedback pin sync registered under the hostname and
died on ``UNIQUE constraint failed: machines.name`` before any hub call, every
tick.
"""
from __future__ import annotations

import socket
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.shared.state import machine_identity
from apps.sync_hub import client as sync_client
from apps.sync_hub import config as sync_config
from apps.sync_hub import maintenance, maintenance_enroll
from apps.sync_hub import service as sync_service
from apps.sync_hub import status as sync_status
from apps.webui.server.app import create_app
from tests.waits import start_uvicorn_in_thread

_HOST = "sharedhost"
_CONFIGURED = "air-preview"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def hub_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    monkeypatch.delenv("MDT_IS_HUB", raising=False)
    monkeypatch.delenv(sync_status.SCHEDULER_ENV, raising=False)
    hub_dir = tmp_path / "hub"
    hub_dir.mkdir()
    state_db.open_rw(sync_client.state_db_path(hub_dir)).close()
    app = FastAPI()
    app.state.state_db_path = str(sync_client.state_db_path(hub_dir))
    app.state.sync_hub_data_dir = str(hub_dir)
    app.state.sync_hub_machine_name = "hub"
    app.include_router(sync_service.router, prefix="/api/v1")
    port = _free_port()
    server, thread = start_uvicorn_in_thread(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"),
        what="the machine-name hub",
    )
    url = f"http://127.0.0.1:{port}"
    monkeypatch.setenv(sync_status.ENDPOINT_ENV, url)
    try:
        yield url
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)


def _open(data_dir: Path) -> sqlite3.Connection:
    return state_db.open_rw(sync_client.state_db_path(data_dir))


def _data_dir_holding_the_hosts_name(tmp_path: Path, *, configured: str | None) -> Path:
    """A data dir whose ``machines`` table already gives ``_HOST`` to ANOTHER id."""
    data_dir = tmp_path / "second-data-dir"
    conn = _open(data_dir)
    try:
        other = machine_identity.register_machine(
            conn, data_dir=tmp_path / "first-data-dir", name=_HOST
        )
        conn.commit()
    finally:
        conn.close()
    assert other.machine_id != machine_identity.get_or_create_machine_id(data_dir)
    if configured is not None:
        sync_config.write_config(
            data_dir,
            sync_config.CloudSyncConfig(enabled=False, hub_url=None, machine_name=configured),
        )
    return data_dir


def _own_name(data_dir: Path) -> str | None:
    conn = _open(data_dir)
    try:
        row = conn.execute(
            "SELECT name FROM machines WHERE machine_id = ?",
            (machine_identity.get_or_create_machine_id(data_dir),),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else str(row[0])


def _engine(data_dir: Path, hostname: str) -> FastAPI:
    app = create_app(
        state_db_path=str(sync_client.state_db_path(data_dir)), hostname=hostname,
        version="9.9.9-test", mount_frontend=False, enable_cors=False,
    )
    app.state.data_dir = data_dir
    return app


def test_feedback_sync_registers_under_the_configured_name_not_the_hostname(
    tmp_path: Path, hub_url: str
) -> None:
    """[if] the hostname is taken by another machine id [then] sync still succeeds
    under the configured name and this data dir's own id, [else stop]."""
    data_dir = _data_dir_holding_the_hosts_name(tmp_path, configured=_CONFIGURED)
    with TestClient(_engine(data_dir, _HOST)) as http:
        response = http.post("/api/v1/feedback/sync")
    assert response.status_code == 200, response.text
    assert _own_name(data_dir) == _CONFIGURED


def test_feedback_sync_without_a_configured_name_keeps_the_engine_hostname(
    tmp_path: Path, hub_url: str
) -> None:
    """Overshoot control: [if] no name is configured [then] the engine's published
    hostname is still the registered name, [else stop]."""
    data_dir = tmp_path / "plain"
    _open(data_dir).close()
    with TestClient(_engine(data_dir, "plainhost")) as http:
        response = http.post("/api/v1/feedback/sync")
    assert response.status_code == 200, response.text
    assert _own_name(data_dir) == "plainhost"


def test_cli_sync_without_name_uses_the_configured_name(tmp_path: Path, hub_url: str) -> None:
    """[if] ``sync`` runs with no --name [then] it keeps the saved config name
    instead of renaming the machine to the hostname, [else stop]."""
    data_dir = _data_dir_holding_the_hosts_name(tmp_path, configured=_CONFIGURED)
    assert maintenance.main(["sync", "--data-dir", str(data_dir), "--hub", hub_url]) == 0
    assert _own_name(data_dir) == _CONFIGURED


def test_cli_sync_explicit_name_beats_the_configured_name(tmp_path: Path, hub_url: str) -> None:
    data_dir = _data_dir_holding_the_hosts_name(tmp_path, configured=_CONFIGURED)
    code = maintenance.main(
        ["sync", "--data-dir", str(data_dir), "--hub", hub_url, "--name", "explicit"]
    )
    assert code == 0
    assert _own_name(data_dir) == "explicit"


def test_cli_sync_without_any_name_still_defaults_to_the_hostname(
    tmp_path: Path, hub_url: str
) -> None:
    """Overshoot control: no --name and no config name registers the short hostname."""
    data_dir = tmp_path / "plain"
    _open(data_dir).close()
    assert maintenance.main(["sync", "--data-dir", str(data_dir), "--hub", hub_url]) == 0
    assert _own_name(data_dir) == machine_identity.default_machine_name()


def test_enroll_introduces_the_machine_under_the_configured_name(tmp_path: Path) -> None:
    data_dir = tmp_path / "enrolling"
    data_dir.mkdir()
    assert (
        maintenance_enroll.local_machine_row(data_dir).name
        == machine_identity.default_machine_name()
    )
    sync_config.write_config(
        data_dir,
        sync_config.CloudSyncConfig(enabled=False, hub_url=None, machine_name=_CONFIGURED),
    )
    assert maintenance_enroll.local_machine_row(data_dir).name == _CONFIGURED
    assert maintenance_enroll.local_machine_row(data_dir, name="explicit").name == "explicit"
