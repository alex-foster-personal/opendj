"""Hub transport backoff for the engine CloudSync scheduler (CLOUDSYNC-20)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI

from apps.shared.state import db as state_db
from apps.sync_hub import client as sync_client
from apps.sync_hub import status as sync_status
from apps.webui.server.cloudsync_scheduler import (
    HUB_ERROR_BASE_S,
    HUB_ERROR_MAX_BACKOFF_S,
    CloudSyncScheduler,
)

_PUSH_502 = (
    "the hub or proxy closed the connection (502; may be Tailscale serve or client timeout): "
    "POST https://hub:8870/api/v1/sync/push -> HTTP 502:  (after 45.0s)"
)


def _app(data_dir: Path) -> FastAPI:
    db_path = sync_client.state_db_path(data_dir)
    state_db.open_rw(db_path, apply_schema=True).close()
    app = FastAPI()
    app.state.state_db_path = str(db_path)
    return app


def _journal_push_error(data_dir: Path) -> None:
    sync_status.write_result(
        data_dir,
        sync_status.SyncResult(
            finished_at="2026-09-15T06:00:00+00:00",
            status="error",
            message=_PUSH_502,
            pushed=0,
            pulled=0,
        ),
    )


@pytest.mark.requirement("CLOUDSYNC-20")
def test_next_wait_s_backoffs_on_hub_transport_error(tmp_path: Path) -> None:
    """[if] the last sync failed on hub transport [then] scheduler waits beyond base interval, [else stop]."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    app = _app(data_dir)
    _journal_push_error(data_dir)
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
    assert first >= HUB_ERROR_BASE_S
    assert first > scheduler._interval_s
    assert second > first
    assert second <= HUB_ERROR_MAX_BACKOFF_S


def test_next_wait_s_does_not_backoff_on_digest_mismatch(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    app = _app(data_dir)
    sync_status.write_result(
        data_dir,
        sync_status.SyncResult(
            finished_at="2026-09-15T06:00:00+00:00",
            status="error",
            message="SyncDigestMismatch: tables ['tracks'] differ",
            pushed=1,
            pulled=1,
        ),
    )
    scheduler = CloudSyncScheduler(
        app,
        interval_s=60.0,
        env={
            sync_status.SCHEDULER_ENV: "1",
            sync_status.ENDPOINT_ENV: "https://hub.example.test",
        },
    )
    assert scheduler._next_wait_s() == scheduler._interval_s
