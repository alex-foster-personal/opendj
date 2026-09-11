"""OAuth callback redirects: return_to preservation and error surfacing."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlparse

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.auth import SESSION_COOKIE_NAME
from tests.webui.test_auth import FAKE_CONFIG, _identity

_ORIGIN = "http://127.0.0.1:9418"


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
    monkeypatch.setenv(
        "OPENDJ_GOOGLE_OAUTH_CLIENT_SECRET", FAKE_CONFIG.client_secret
    )


def _start_login(
    client: TestClient, return_to: str | None = None
) -> dict[str, str]:
    body: dict[str, str] = {"origin": _ORIGIN}
    if return_to is not None:
        body["return_to"] = return_to
    response = client.post("/api/v1/auth/login", json=body)
    assert response.status_code == 200
    return response.json()


def test_callback_google_error_redirects_to_return_to_with_server_message(
    client: TestClient,
) -> None:
    login = _start_login(client, return_to="/performance")
    response = client.get(
        "/api/v1/auth/callback",
        params={"state": login["state"], "error": "access_denied"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    parsed = urlparse(location)
    assert parsed.scheme == "http"
    assert parsed.netloc == "127.0.0.1:9418"
    assert parsed.path == "/performance"
    error_value = parse_qs(parsed.query)["opendj_auth_error"][0]
    assert unquote(error_value) == "Google refused the sign-in: access_denied"
    assert SESSION_COOKIE_NAME not in response.headers.get("set-cookie", "")


def test_callback_success_redirects_to_performance_and_sets_cookie(
    client: TestClient,
) -> None:
    login = _start_login(client, return_to="/performance")
    with patch(
        "apps.webui.server.routes.auth.exchange_code",
        return_value=_identity(),
    ):
        response = client.get(
            "/api/v1/auth/callback",
            params={"state": login["state"], "code": "fake"},
            follow_redirects=False,
        )
    assert response.status_code == 303
    location = response.headers["location"]
    parsed = urlparse(location)
    assert location == f"{_ORIGIN}/performance"
    assert "opendj_auth_error" not in parsed.query
    assert SESSION_COOKIE_NAME in response.headers.get("set-cookie", "")


@pytest.mark.parametrize(
    "return_to",
    [
        "https://evil.example.com",
        "//evil.example.com",
        "/\tevil",
    ],
)
def test_login_rejects_or_sanitizes_an_open_redirect_return_to(
    client: TestClient, return_to: str
) -> None:
    login = _start_login(client, return_to=return_to)
    with patch(
        "apps.webui.server.routes.auth.exchange_code",
        return_value=_identity(),
    ):
        response = client.get(
            "/api/v1/auth/callback",
            params={"state": login["state"], "code": "fake"},
            follow_redirects=False,
        )
    assert response.status_code == 303
    location = response.headers["location"]
    parsed = urlparse(location)
    assert parsed.netloc == "127.0.0.1:9418"
    assert parsed.path == "/"
    assert "evil" not in location


pytestmark = pytest.mark.rb_parity
