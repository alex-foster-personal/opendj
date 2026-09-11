"""GET /cloudsync/machines self-registers under the CONFIGURED machine name.

Found by the /cloudsync UI e2e (plan W17): Sync now renames this machine to
``cloudsync-config.json``'s ``machine_name``, but the page's next load
self-registered with the hostname again. That renames the machine back, and
when the hub runs on the same host (its row, pulled into this DB, already
holds the hostname) it 500s on ``UNIQUE machines.name``. The configured name
is the one persistent source the scheduler and Sync now both use, so the
self-registration must use it too.

Regression one-liners:
  - [if] a configured machine_name is ignored by self-registration [then] broken, [else stop]
  - [if] a same-host peer holding the hostname makes GET /machines 500 [then] broken, [else stop]
  - [if] a malformed config silently falls back to the hostname [then] broken, [else stop]
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.shared.state.machine_identity import default_machine_name
from apps.sync_hub import config as sync_config
from apps.webui.server.app import create_app
from apps.webui.server.routes import cloudsync as cloudsync_routes
from apps.webui.server.sqlite_backend import SqliteBackend

pytestmark = pytest.mark.requirement("CAT-04")

CONFIGURED_NAME: str = "gig-rig"
PEER_MACHINE_ID: str = "f" * 32


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    state_db.open_rw(tmp_path / "state" / "state.db", apply_schema=True).close()
    return tmp_path


@pytest.fixture
def client(data_dir: Path) -> Iterator[TestClient]:
    db_path = data_dir / "state" / "state.db"
    app = create_app(
        backend=SqliteBackend(db_path),
        bind_host="127.0.0.1",
        hostname="test-host",
        state_db_path=str(db_path),
        mount_frontend=False,
    )
    app.include_router(cloudsync_routes.router, prefix="/api/v1")
    with TestClient(app) as test_client:
        yield test_client


def _configure_name(data_dir: Path, name: str | None) -> None:
    sync_config.write_config(
        data_dir, sync_config.CloudSyncConfig(enabled=False, hub_url=None, machine_name=name)
    )


def _insert_same_host_peer(data_dir: Path) -> None:
    """A hub on this same host, as pulled into this DB: it holds the hostname."""
    conn = sqlite3.connect(data_dir / "state" / "state.db")
    try:
        conn.execute(
            "INSERT INTO machines(machine_id, name, platform, is_hub, data_root, "
            "first_seen, last_seen) VALUES (?, ?, 'macos', 1, NULL, ?, ?)",
            (
                PEER_MACHINE_ID,
                default_machine_name(),
                "2026-09-11T00:00:00.000000+00:00",
                "2026-09-11T00:00:00.000000+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _local_row(rows: list[dict[str, object]]) -> dict[str, object]:
    local = [row for row in rows if row["machine_id"] != PEER_MACHINE_ID]
    assert len(local) == 1, rows
    return local[0]


def test_self_registration_uses_the_configured_machine_name(
    client: TestClient, data_dir: Path
) -> None:
    """if GET /machines registers the hostname while a machine_name is configured then broken"""
    _configure_name(data_dir, CONFIGURED_NAME)
    response = client.get("/api/v1/cloudsync/machines")
    assert response.status_code == 200, response.text
    assert _local_row(response.json())["name"] == CONFIGURED_NAME


def test_same_host_peer_holding_the_hostname_does_not_500(
    client: TestClient, data_dir: Path
) -> None:
    """if a same-host hub row holding the hostname makes GET /machines 500 then broken"""
    _configure_name(data_dir, CONFIGURED_NAME)
    _insert_same_host_peer(data_dir)
    response = client.get("/api/v1/cloudsync/machines")
    assert response.status_code == 200, response.text
    names = sorted(str(row["name"]) for row in response.json())
    assert names == sorted([CONFIGURED_NAME, default_machine_name()])


def test_no_configured_name_still_registers_the_hostname(
    client: TestClient, data_dir: Path
) -> None:
    """if an unconfigured machine stops self-registering under its hostname then broken"""
    _configure_name(data_dir, None)
    response = client.get("/api/v1/cloudsync/machines")
    assert response.status_code == 200, response.text
    assert _local_row(response.json())["name"] == default_machine_name()


def test_malformed_config_fails_loudly_instead_of_using_the_hostname(
    client: TestClient, data_dir: Path
) -> None:
    """if a malformed cloudsync-config.json silently registers the hostname then broken"""
    sync_config.config_path(data_dir).write_text("{not json", encoding="utf-8")
    response = client.get("/api/v1/cloudsync/machines")
    assert response.status_code == 500, response.text
    assert response.json()["detail"]["code"] == "CLOUDSYNC_CONFIG_INVALID"
