"""Wire-mismatch backoff for the engine CloudSync scheduler."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI

from apps.shared.state import db as state_db
from apps.sync_hub import client as sync_client
from apps.sync_hub import status as sync_status
from apps.webui.server.cloudsync_scheduler import (
    WIRE_MISMATCH_BASE_S,
    WIRE_MISMATCH_MAX_BACKOFF_S,
    CloudSyncScheduler,
)

_SILVER_MESSAGE = (
    'POST https://agentbox.<tailnet>:8870/api/v1/sync/hello -> HTTP 409: '
    '{"detail":{"code":"SYNC_WIRE_VERSION","message":"peer speaks sync wire v4, '
    'this machine speaks v3 (schema v14 vs v13). The synced row shapes differ, '
    'so no row may cross; upgrade whichever machine is on the lower wire version, '
    'then sync again."}}'
)


def _app(data_dir: Path) -> FastAPI:
    db_path = sync_client.state_db_path(data_dir)
    state_db.open_rw(db_path, apply_schema=True).close()
    app = FastAPI()
    app.state.state_db_path = str(db_path)
    return app


def _journal_wire_mismatch(data_dir: Path) -> None:
    sync_status.write_result(
        data_dir,
        sync_status.SyncResult(
            finished_at="2026-09-15T06:00:00+00:00",
            status="error",
            message=_SILVER_MESSAGE,
            pushed=0,
            pulled=0,
        ),
    )


@pytest.mark.requirement("CSSTATUS-06")
def test_next_wait_s_backoffs_beyond_base_interval_when_update_required(
    tmp_path: Path,
) -> None:
    """[if] wire versions still differ [then] scheduled sync attempts back off beyond 60s."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    app = _app(data_dir)
    _journal_wire_mismatch(data_dir)
    scheduler = CloudSyncScheduler(
        app,
        interval_s=0.05,
        env={
            sync_status.SCHEDULER_ENV: "1",
            sync_status.ENDPOINT_ENV: "https://hub.example.test",
        },
    )
    first = scheduler._next_wait_s()
    second = scheduler._next_wait_s()
    assert first >= WIRE_MISMATCH_BASE_S
    assert first > scheduler._interval_s
    assert second > first
    assert second <= WIRE_MISMATCH_MAX_BACKOFF_S


def test_next_wait_s_resets_after_wire_mismatch_clears(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    app = _app(data_dir)
    _journal_wire_mismatch(data_dir)
    scheduler = CloudSyncScheduler(
        app,
        interval_s=0.05,
        env={
            sync_status.SCHEDULER_ENV: "1",
            sync_status.ENDPOINT_ENV: "https://hub.example.test",
        },
    )
    assert scheduler._next_wait_s() >= WIRE_MISMATCH_BASE_S
    sync_status.write_result(
        data_dir,
        sync_status.SyncResult(
            finished_at="2026-09-15T06:01:00+00:00",
            status="ok",
            message="completed",
            pushed=1,
            pulled=1,
        ),
    )
    assert scheduler._next_wait_s() == scheduler._interval_s
