"""A hub request must not checkpoint and delete the WAL when it closes (LIBM-120 L6).

Each hub request opens its own connection. When that close was the last
one on the database, SQLite checkpointed the whole WAL, synced it and
deleted the file, and the next request synced a new one: 5.8 s of a
10,000-track first sync on agentbox. The instrument is the WAL file itself,
read after every request: present means the close was not the last one.

[if] a hub request's close deletes the WAL [then] checkpoint per request, [else stop].

Controls: the probe must see the WAL vanish when the keeper is removed; the
keeper must not hold a read transaction, or no checkpoint could ever reset
the WAL; one keeper per database, however many requests.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.sync_hub import client, hub_wal_keeper, service
from tests.cloudsync.enrollment_transport import TestClientTransport

pytestmark = pytest.mark.requirement("LIBM-120")

SYNCS = 3


@pytest.fixture
def hub_app(tmp_path: Path) -> FastAPI:
    app = FastAPI()
    app.state.state_db_path = str(client.state_db_path(tmp_path / "hub"))
    app.state.sync_hub_data_dir = str(tmp_path / "hub")
    app.state.sync_hub_machine_name = "hub"
    app.include_router(service.router, prefix="/api/v1")
    return app


def _wal(app: FastAPI) -> Path:
    return Path(f"{app.state.state_db_path}-wal")


def _wal_after_each_sync(app: FastAPI, spoke: Path) -> Iterator[bool]:
    with TestClient(app) as http:
        for _ in range(SYNCS):
            client.run_sync(
                spoke, "http://hub.invalid", transport=TestClientTransport(http), name="spoke"
            )
            yield _wal(app).exists()


def test_the_hub_wal_outlives_every_request(hub_app: FastAPI, tmp_path: Path) -> None:
    present = list(_wal_after_each_sync(hub_app, tmp_path / "spoke"))
    assert present == [True] * SYNCS, (
        f"hub WAL present after each sync: {present}. A request's close() was "
        "the last connection, so it checkpointed and deleted the WAL (LIBM-120 L6)."
    )


def test_probe_sees_the_wal_vanish_without_a_keeper(
    hub_app: FastAPI, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative control: without the keeper the same probe must go red."""
    monkeypatch.setattr(hub_wal_keeper, "keep_wal_open", lambda *_args: None)
    present = list(_wal_after_each_sync(hub_app, tmp_path / "spoke"))
    assert present == [False] * SYNCS, present


def test_the_keeper_never_blocks_a_checkpoint(hub_app: FastAPI, tmp_path: Path) -> None:
    """Overshoot control: a keeper holding a read transaction would keep the WAL
    too, and pass the test above, while no checkpoint could ever reset it."""
    list(_wal_after_each_sync(hub_app, tmp_path / "spoke"))
    probe = sqlite3.connect(hub_app.state.state_db_path)
    try:
        busy, log_frames, checkpointed = probe.execute(
            "PRAGMA wal_checkpoint(TRUNCATE)"
        ).fetchone()
    finally:
        probe.close()
    assert (busy, log_frames, checkpointed) == (0, 0, 0)


def test_one_keeper_per_database(hub_app: FastAPI, tmp_path: Path) -> None:
    list(_wal_after_each_sync(hub_app, tmp_path / "spoke"))
    keepers = hub_wal_keeper.wal_keepers(hub_app.state)
    assert list(keepers) == [str(Path(hub_app.state.state_db_path).resolve())]


def _leave_wal(app: FastAPI) -> str:
    probe = sqlite3.connect(app.state.state_db_path)
    try:
        return str(probe.execute("PRAGMA journal_mode = DELETE").fetchone()[0])
    finally:
        probe.close()


def test_a_keeper_is_why_the_database_is_not_exclusive(hub_app: FastAPI, tmp_path: Path) -> None:
    """The keeper's one cost, pinned so nobody meets it as a surprise: while it
    is open, nothing else can take the database to itself."""
    with TestClient(hub_app) as http:
        client.run_sync(
            tmp_path / "spoke", "http://hub.invalid", transport=TestClientTransport(http),
            name="spoke",
        )
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            _leave_wal(hub_app)
        assert hub_wal_keeper.close_wal_keepers(hub_app.state) == 1
        assert _leave_wal(hub_app) == "delete"
        assert hub_wal_keeper.wal_keepers(hub_app.state) == {}

