"""The daemon's offload sweep timer only runs when cloud offload is configured.

* [if] R2 credentials are absent [then] ``arm()`` starts no sweep timer and
  logs that offload is not configured, instead of an error every interval;
* [if] R2 credentials are present [then] ``arm()`` starts the timer and it
  queues a sweep.

[if] arm() starts a sweep timer without R2 credentials [then] fail, [else stop].
"""

from __future__ import annotations

import logging
import time

import pytest
from fastapi import FastAPI

from apps.cloud.config import REQUIRED_VARS
from apps.webui.server.cloud_offload_service import OffloadService

pytestmark = pytest.mark.requirement("OFFLOAD-9231")

R2_ENV = {"R2_ACCOUNT_ID": "acct", "R2_ACCESS_KEY_ID": "AKID", "R2_SECRET_ACCESS_KEY": "secret"}


def test_arm_without_credentials_starts_no_sweep(monkeypatch, caplog):
    for name in REQUIRED_VARS:
        monkeypatch.delenv(name, raising=False)
    service = OffloadService(FastAPI())
    caplog.set_level(logging.INFO, logger="apps.webui.server.cloud_offload_service")
    try:
        assert service.arm(first_delay_s=0) is False
        assert service._sweep_timer is None
        assert service.operations() == []
        assert "not configured" in caplog.text
        assert "sweep not submitted" not in caplog.text
    finally:
        service.stop()


def test_arm_with_credentials_queues_a_sweep(monkeypatch, tmp_path):
    for name, value in R2_ENV.items():
        monkeypatch.setenv(name, value)
    app = FastAPI()
    app.state.state_db_path = str(tmp_path / "data" / "state" / "state.db")
    service = OffloadService(app)
    # Keep the queued sweep from running against a real object store.
    monkeypatch.setattr(service, "run", lambda op: None)
    monkeypatch.setattr(service, "resume", list)
    try:
        assert service.arm(first_delay_s=0) is True
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not service.operations():
            time.sleep(0.01)
        assert [op.kind for op in service.operations()] == ["sweep"]
    finally:
        service.stop()
