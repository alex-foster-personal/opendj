"""FB-09 (pin 863616aa65dd): comment pin environment provenance and UTC diagnostics."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from apps.webui.run_agentbox import dated_log_path
from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend
from apps.webui.server.client_logs import daily_log_path


@pytest.fixture
def fb(tmp_path: Path) -> TestClient:
    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True)
    app = create_app(
        backend=InMemoryBackend(),
        bind_host="127.0.0.1",
        hostname="test-host",
        mount_frontend=False,
    )
    app.state.data_dir = data_dir
    with TestClient(app) as client:
        yield client


@pytest.mark.requirement("FB-09")
def test_pin_863616aa65dd_comment_carries_environment_and_utc_created_at(
    fb: TestClient,
) -> None:
    """[if] pin drops with viewport facts [then] environment and UTC stamp return, [else stop]."""
    r = fb.post(
        "/api/v1/feedback/comments",
        json={
            "x_pct": 10,
            "y_pct": 20,
            "page": "/performance",
            "text": "pin provenance",
            "ui": "chrome-loop",
            "viewport_width": 1280,
            "viewport_height": 800,
        },
    )
    assert r.status_code == 201
    pin = r.json()
    assert pin["created_at"].endswith("Z"), "stored pin timestamps must be UTC Z-suffixed"
    assert pin["environment"] == {
        "ui": "chrome-loop",
        "viewport_width": 1280,
        "viewport_height": 800,
        "machine": "test-host",
        "release_version": "0.1.0",
    }
    git_sha = pin["build"]["git_sha"]
    assert git_sha
    assert git_sha != pin["environment"]["release_version"]


@pytest.mark.requirement("FB-09")
def test_pin_863616aa65dd_packaged_app_ui_kind(fb: TestClient) -> None:
    """[if] packaged-app ui is posted [then] environment.ui is packaged-app, [else stop]."""
    r = fb.post(
        "/api/v1/feedback/comments",
        json={
            "x_pct": 1,
            "y_pct": 1,
            "page": "/performance",
            "text": "wkwebview pin",
            "ui": "packaged-app",
            "viewport_width": 1440,
            "viewport_height": 900,
        },
    )
    assert r.status_code == 201
    assert r.json()["environment"]["ui"] == "packaged-app"


@pytest.mark.requirement("FB-09")
def test_pin_863616aa65dd_audited_log_filenames_use_utc_calendar_day(
    tmp_path: Path,
) -> None:
    """[if] local and UTC calendar days differ [then] audited logs name the UTC day, [else stop]."""
    utc_day = time.strptime("2026-08-17", "%Y-%m-%d")
    local_day = time.strptime("2026-08-18", "%Y-%m-%d")
    for prefix in (
        "webui-visitors",
        "webui-client-errors",
        "webui-performance",
        "webui-backend",
    ):
        assert daily_log_path(tmp_path, prefix, utc_day).name == f"{prefix}-2026-08-17.log"
        assert (
            daily_log_path(tmp_path, prefix, local_day).name == f"{prefix}-2026-08-18.log"
        ), "callers must pass gmtime(), not localtime()"
    assert dated_log_path("webui-backend", durable_dir=tmp_path, now=utc_day).name == (
        "webui-backend-2026-08-17.log"
    )


@pytest.mark.requirement("FB-09")
def test_pin_863616aa65dd_visitor_log_uses_gmtime_filename(tmp_path: Path) -> None:
    """[if] visitor event is stored near midnight [then] log file uses UTC day, [else stop]."""
    app = create_app(
        backend=InMemoryBackend(),
        mount_frontend=False,
        enable_cors=False,
        client_event_log_dir=tmp_path,
    )
    utc_struct = time.strptime("2026-08-17", "%Y-%m-%d")
    with (
        patch("apps.webui.server.routes.client_events.time.gmtime", return_value=utc_struct),
        TestClient(app) as client,
    ):
        response = client.post(
            "/api/v1/client-events",
            json={
                "client_event_id": "fb09-utc",
                "kind": "page-view",
                "url": "https://agentbox.example.ts.net/performance",
                "path": "/performance",
                "referrer": "https://agentbox.example.ts.net/",
                "client_timestamp": "2026-08-17T07:00:00.000Z",
                "user_agent": "test",
                "language": "en",
                "secure_context": True,
                "viewport_width": 1280,
                "viewport_height": 800,
            },
        )
        assert response.status_code == 202
        paths = list(tmp_path.glob("webui-visitors-*.log"))
        assert paths == [tmp_path / "webui-visitors-2026-08-17.log"]
