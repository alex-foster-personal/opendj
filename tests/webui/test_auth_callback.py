"""OAuth callback redirects: return_to preservation and error surfacing."""

from __future__ import annotations

from collections.abc import Iterator
from http.cookies import SimpleCookie
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlparse

import pytest
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.webui.server.app import create_app
from apps.webui.server.auth import SESSION_COOKIE_NAME, SESSION_TTL, SessionStore
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


def test_session_cookie_attributes_are_host_only_lax_path_root(
    client: TestClient,
) -> None:
    """[if] the OAuth callback plants opendj_session [then] Set-Cookie is Path=/ SameSite=Lax HttpOnly host-only and not Secure, [else stop]."""
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
    raw = response.headers.get("set-cookie", "")
    assert f"{SESSION_COOKIE_NAME}=" in raw

    cookie = SimpleCookie()
    cookie.load(raw)
    morsel = cookie[SESSION_COOKIE_NAME]
    assert morsel["path"] == "/"
    assert morsel["samesite"].lower() == "lax"
    assert morsel["httponly"]
    assert not morsel["secure"]
    assert morsel["domain"] == ""
    assert int(morsel["max-age"]) == int(SESSION_TTL.total_seconds())


def test_auth_me_same_cookie_same_identity_on_shared_backend(
    state_db_path: Path,
) -> None:
    """[if] two clients present the same opendj_session to /auth/me [then] both receive the same google_sub, and a third client with no cookie is signed out, [else stop]."""
    store = SessionStore(state_db_path)
    token = store.sign_in(_identity())
    assert store.resolve(token) is not None

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

    with TestClient(app) as client_a:
        client_a.cookies.set(SESSION_COOKIE_NAME, token)
        response_a = client_a.get("/api/v1/auth/me")
        assert response_a.status_code == 200
        body_a = response_a.json()
        assert body_a["signed_in"] is True

    with TestClient(app) as client_b:
        client_b.cookies.set(SESSION_COOKIE_NAME, token)
        response_b = client_b.get("/api/v1/auth/me")
        assert response_b.status_code == 200
        body_b = response_b.json()
        assert body_b["signed_in"] is True
        assert body_a["user"]["google_sub"] == body_b["user"]["google_sub"]
        assert body_a["user"]["email"] == body_b["user"]["email"]

    with TestClient(app) as client_c:
        response_c = client_c.get("/api/v1/auth/me")
        assert response_c.status_code == 200
        assert response_c.json() == {"signed_in": False, "user": None}
        assert store.resolve(token) is not None


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
