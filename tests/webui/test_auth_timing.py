"""Positive controls for auth route timing (S13 server phases)."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.auth import GoogleIdentity, GoogleOAuthConfig
from apps.webui.server.auth_timing import inject_delay, last_capture, reset_inject_delay
from tests.webui.test_auth import FAKE_CONFIG, _identity


@pytest.fixture(autouse=True)
def _clear_auth_delay() -> Iterator[None]:
    reset_inject_delay()
    yield
    reset_inject_delay()


@pytest.fixture
def state_db_path(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    state_db.open_rw(path).close()
    return path


@pytest.fixture
def client(state_db_path: Path) -> Iterator[TestClient]:
    app = create_app(
        bind_host="127.0.0.1",
        hostname="test-host",
        port=18697,
        frontend_port=19411,
        state_db_path=str(state_db_path),
        lock_status_fn=lambda: None,
        syncthing_status_fn=lambda: None,
        mount_frontend=False,
    )
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _fake_oauth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENDJ_GOOGLE_OAUTH_CLIENT_ID", FAKE_CONFIG.client_id)
    monkeypatch.setenv("OPENDJ_GOOGLE_OAUTH_CLIENT_SECRET", FAKE_CONFIG.client_secret)


def test_login_delay_increases_measured_duration(client: TestClient) -> None:
    inject_delay(0.2)
    response = client.post(
        "/api/v1/auth/login",
        json={"origin": "http://127.0.0.1:19411"},
    )
    assert response.status_code == 200
    assert int(response.headers["X-OpenDJ-Auth-Ms"]) >= 200
    capture = last_capture()
    assert capture is not None
    assert capture["op"] == "login"
    assert capture["duration_ms"] >= 200


def test_callback_delay_increases_measured_duration(client: TestClient) -> None:
    login = client.post(
        "/api/v1/auth/login",
        json={"origin": "http://127.0.0.1:19411"},
    ).json()

    def _slow_exchange(
        _config: GoogleOAuthConfig, _pending: object, _code: str
    ) -> GoogleIdentity:
        time.sleep(0.2)
        return _identity()

    with patch("apps.webui.server.routes.auth.exchange_code", _slow_exchange):
        response = client.get(
            "/api/v1/auth/callback",
            params={"state": login["state"], "code": "fake-code"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    capture = last_capture()
    assert capture is not None
    assert capture["op"] == "callback"
    assert capture["duration_ms"] >= 200
