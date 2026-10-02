"""Routes for the on-device library fingerprint scan (POST/GET /dedup/scan).

[if] a scan is started [then] one runs at a time and its outcome is reported, [else stop].

Regression one-liners:
  - if a second POST while a scan runs starts another one then broken
  - if a failed scan reports as done, or cannot be started again, then broken
"""
from __future__ import annotations

from typing import Any

import pytest

from apps.webui.server.backend import InMemoryBackend

pytestmark = [pytest.mark.requirement("META-09")]


@pytest.fixture
def app_client():
    from fastapi.testclient import TestClient

    from apps.webui.server.app import create_app

    app = create_app(
        backend=InMemoryBackend(), bind_host="127.0.0.1", hostname="test-host",
        lock_status_fn=lambda: None,
    )
    with TestClient(app) as c:
        yield c


def test_scan_starts_once_reports_progress_and_finishes(app_client, monkeypatch) -> None:
    import threading

    from apps.dedup import library_scan

    release = threading.Event()
    calls: list[dict[str, Any]] = []

    def runner(*, progress, **kwargs):
        calls.append(kwargs)
        progress.total = 3
        progress.done = 1
        release.wait(10)
        progress.done = 3
        progress.computed = 3
        progress.clusters = 1
        return progress

    job = library_scan.LibraryScanJob(runner=runner)
    monkeypatch.setattr(library_scan, "JOB", job)

    assert app_client.get("/api/v1/dedup/scan").json()["state"] == "idle"
    first = app_client.post("/api/v1/dedup/scan")
    assert first.status_code == 202
    assert first.json()["state"] == "running"
    # A second start while one runs is refused, and starts nothing.
    second = app_client.post("/api/v1/dedup/scan")
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "scan_running"
    assert app_client.get("/api/v1/dedup/scan").json()["done"] == 1

    release.set()
    job.join(10)
    body = app_client.get("/api/v1/dedup/scan").json()
    assert (body["state"], body["done"], body["clusters"]) == ("done", 3, 1)
    assert body["finished_at"] is not None
    assert len(calls) == 1
    # The scan reads this app's own state database, not the process default,
    # and writes clusters where the review routes read them.
    from pathlib import Path

    from apps.webui.server.dedup_review_ops import dedup_db_path

    assert calls[0] == {
        "state_db": Path(app_client.app.state.state_db_path),
        "db_path": dedup_db_path(),
    }


def test_scan_failure_is_reported_not_swallowed(app_client, monkeypatch) -> None:
    from apps.dedup import library_scan
    from apps.shared.fingerprints import ChromaprintMissing

    def runner(*, progress, **kwargs):
        raise ChromaprintMissing("no engine build")

    job = library_scan.LibraryScanJob(runner=runner)
    monkeypatch.setattr(library_scan, "JOB", job)
    assert app_client.post("/api/v1/dedup/scan").status_code == 202
    job.join(10)
    body = app_client.get("/api/v1/dedup/scan").json()
    assert body["state"] == "failed"
    assert "no engine build" in body["error"]
    # A failed scan can be started again.
    assert app_client.post("/api/v1/dedup/scan").status_code == 202
    job.join(10)
