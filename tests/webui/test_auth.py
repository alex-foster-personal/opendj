"""Google sign-in: session round-trip, CSRF state, schema FKs, config gate.

Deliberately NOT covered here: a green end-to-end sign-in. Faking Google's
token endpoint would prove only that the mock answers, so the live flow is
verified by hand against real Google. What is covered is everything that
can be checked honestly without the network:

  - if GET /auth/me returns anything but 200 {signed_in: false, user: null}
    while signed out, broken (401 is a fault, not the signed-out answer)
  - if a session cookie does not survive a fresh SessionStore over the same
    file, persistence across a daemon restart is broken
  - if a replayed or unknown OAuth `state` is accepted, CSRF defence is broken
  - if auth_sessions accepts an unknown google_sub, the FK is not enforced
  - if deleting a user leaves its sessions behind, the CASCADE is broken
  - if POST /auth/login answers anything but 503 with no credentials set,
    the fail-fast credential gate is broken
"""
from __future__ import annotations

import base64
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from apps.shared.state import db as state_db
from apps.webui.server import auth as auth_mod
from apps.webui.server.app import create_app
from apps.webui.server.auth import (
    SESSION_COOKIE_NAME,
    AuthConfigError,
    AuthFlowError,
    GoogleIdentity,
    GoogleOAuthConfig,
    PendingLoginStore,
    SessionStore,
    build_authorization_url,
    code_challenge_for,
    hash_session_token,
    identity_from_token_response,
)
from apps.webui.server.routes import auth as auth_routes

FAKE_CONFIG = GoogleOAuthConfig(
    client_id="test-client.apps.googleusercontent.com",
    client_secret="test-secret",
)


def _identity(sub: str = "sub-123") -> GoogleIdentity:
    return GoogleIdentity(
        google_sub=sub,
        email=f"{sub}@example.com",
        name="Test User",
        avatar_url="https://lh3.googleusercontent.com/test",
        refresh_token="refresh-token-value",
        access_token="access-token-value",
        access_expires_at="2099-01-01T00:00:00+00:00",
    )


@pytest.fixture
def state_db_path(tmp_path: Path) -> Path:
    """A throwaway state DB, migrated to the current schema version."""
    path = tmp_path / "state.db"
    state_db.open_rw(path).close()
    return path


@pytest.fixture
def store(state_db_path: Path) -> SessionStore:
    return SessionStore(state_db_path)


@pytest.fixture
def client(state_db_path: Path) -> Iterator[TestClient]:
    app = create_app(
        bind_host="127.0.0.1", hostname="test-host",
        port=18697, frontend_port=19411,
        state_db_path=str(state_db_path),
        lock_status_fn=lambda: None, syncthing_status_fn=lambda: None,
        mount_frontend=False,
    )
    with TestClient(app) as c:
        yield c


# ----- /auth/me while signed out -----------------------------------------


def test_me_is_200_signed_out_when_there_is_no_cookie(client: TestClient) -> None:
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 200
    body = response.json()
    assert body == {"signed_in": False, "user": None}
    assert "detail" not in body


def test_me_is_200_signed_out_for_an_unknown_cookie(client: TestClient) -> None:
    client.cookies.set(SESSION_COOKIE_NAME, "not-a-real-session-token")
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 200
    body = response.json()
    assert body == {"signed_in": False, "user": None}
    assert "detail" not in body


# ----- session cookie round-trip ------------------------------------------


def test_session_round_trip_over_http(
    client: TestClient, store: SessionStore
) -> None:
    token = store.sign_in(_identity())
    client.cookies.set(SESSION_COOKIE_NAME, token)
    body = client.get("/api/v1/auth/me").json()
    assert body["signed_in"] is True
    assert body["user"]["google_sub"] == "sub-123"
    assert body["user"]["email"] == "sub-123@example.com"
    assert body["user"]["avatar_url"] == "https://lh3.googleusercontent.com/test"


def test_me_never_leaks_tokens(client: TestClient, store: SessionStore) -> None:
    token = store.sign_in(_identity())
    client.cookies.set(SESSION_COOKIE_NAME, token)
    body = client.get("/api/v1/auth/me").json()
    assert "refresh_token" not in body and "refresh_token" not in body["user"]
    assert "access_token" not in body and "access_token" not in body["user"]


def test_session_survives_a_new_store_over_the_same_file(
    state_db_path: Path,
) -> None:
    """Proxy for a daemon restart: state lives in the file, not the process."""
    token = SessionStore(state_db_path).sign_in(_identity())
    reopened = SessionStore(state_db_path)
    user = reopened.resolve(token)
    assert user is not None
    assert user.email == "sub-123@example.com"


def test_only_the_token_hash_is_persisted(
    store: SessionStore, state_db_path: Path
) -> None:
    token = store.sign_in(_identity())
    conn = sqlite3.connect(state_db_path)
    try:
        rows = conn.execute(
            "SELECT session_token_sha256 FROM auth_sessions"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [(hash_session_token(token),)]
    assert token not in {row[0] for row in rows}


def test_logout_drops_the_session(
    client: TestClient, store: SessionStore
) -> None:
    token = store.sign_in(_identity())
    client.cookies.set(SESSION_COOKIE_NAME, token)
    assert client.post("/api/v1/auth/logout").status_code == 204
    assert store.resolve(token) is None


def test_logout_is_idempotent_when_signed_out(client: TestClient) -> None:
    assert client.post("/api/v1/auth/logout").status_code == 204


def test_signing_in_twice_keeps_one_user_and_two_sessions(
    store: SessionStore, state_db_path: Path
) -> None:
    first = store.sign_in(_identity())
    second = store.sign_in(_identity())
    assert first != second
    conn = sqlite3.connect(state_db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM auth_sessions"
        ).fetchone()[0] == 2
    finally:
        conn.close()


def test_expired_session_resolves_to_none_and_is_deleted(
    store: SessionStore, state_db_path: Path
) -> None:
    token = store.sign_in(_identity())
    conn = state_db.open_rw(state_db_path)
    try:
        conn.execute(
            "UPDATE auth_sessions SET expires_at = ?",
            ("2000-01-01T00:00:00+00:00",),
        )
    finally:
        conn.close()
    assert store.resolve(token) is None
    conn = sqlite3.connect(state_db_path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM auth_sessions"
        ).fetchone()[0] == 0
    finally:
        conn.close()


# ----- schema: foreign keys ----------------------------------------------


def test_auth_session_rejects_an_unknown_user(state_db_path: Path) -> None:
    conn = state_db.open_rw(state_db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO auth_sessions(
                    session_token_sha256, google_sub, created_at,
                    last_seen_at, expires_at
                ) VALUES ('hash', 'nobody', 'now', 'now', '2099-01-01T00:00:00')
                """
            )
    finally:
        conn.close()


def test_deleting_a_user_cascades_to_sessions(
    store: SessionStore, state_db_path: Path
) -> None:
    store.sign_in(_identity())
    conn = state_db.open_rw(state_db_path)
    try:
        conn.execute("DELETE FROM users WHERE google_sub = 'sub-123'")
        remaining = conn.execute(
            "SELECT COUNT(*) FROM auth_sessions"
        ).fetchone()[0]
    finally:
        conn.close()
    assert remaining == 0


def test_email_is_unique_across_users(state_db_path: Path) -> None:
    conn = state_db.open_rw(state_db_path)
    try:
        conn.execute(
            "INSERT INTO users(google_sub, email, created_at, updated_at) "
            "VALUES ('a', 'dup@example.com', 'now', 'now')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO users(google_sub, email, created_at, updated_at) "
                "VALUES ('b', 'dup@example.com', 'now', 'now')"
            )
    finally:
        conn.close()


# ----- CSRF state + PKCE --------------------------------------------------


def test_unknown_state_is_rejected() -> None:
    with pytest.raises(AuthFlowError):
        PendingLoginStore().consume("never-issued")


def test_state_is_single_use() -> None:
    pending_logins = PendingLoginStore()
    pending = pending_logins.create("http://127.0.0.1:9418/api/v1/auth/callback")
    pending_logins.consume(pending.state)
    with pytest.raises(AuthFlowError):
        pending_logins.consume(pending.state)


def test_expired_state_is_rejected() -> None:
    pending_logins = PendingLoginStore(ttl_seconds=0)
    pending = pending_logins.create("http://127.0.0.1:9418/api/v1/auth/callback")
    with pytest.raises(AuthFlowError):
        pending_logins.consume(pending.state)


def test_each_login_gets_a_distinct_state_and_verifier() -> None:
    pending_logins = PendingLoginStore()
    first = pending_logins.create("http://127.0.0.1:9418/api/v1/auth/callback")
    second = pending_logins.create("http://127.0.0.1:9418/api/v1/auth/callback")
    assert first.state != second.state
    assert first.code_verifier != second.code_verifier


def test_pkce_challenge_matches_rfc7636_vector() -> None:
    """Appendix B of RFC 7636. A wrong challenge fails only at Google."""
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert code_challenge_for(verifier) == (
        "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    )


def test_authorization_url_carries_pkce_and_offline_access() -> None:
    pending = PendingLoginStore().create(
        "http://127.0.0.1:9418/api/v1/auth/callback"
    )
    url = build_authorization_url(FAKE_CONFIG, pending)
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "code_challenge_method=S256" in url
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert "scope=openid+email+profile" in url
    assert pending.code_verifier not in url


def test_callback_rejects_a_forged_state(client: TestClient) -> None:
    response = client.get(
        "/api/v1/auth/callback",
        params={"state": "forged", "code": "irrelevant"},
        follow_redirects=False,
    )
    assert response.status_code in (400, 503)


def test_callback_without_code_or_state_is_400(client: TestClient) -> None:
    response = client.get("/api/v1/auth/callback", follow_redirects=False)
    assert response.status_code == 400


# ----- credential gate ----------------------------------------------------


def test_config_from_env_raises_a_runbook_when_unset() -> None:
    with pytest.raises(AuthConfigError) as excinfo:
        GoogleOAuthConfig.from_env({})
    message = str(excinfo.value)
    assert "GOOGLE_OAUTH_CLIENT_ID" in message
    assert "Desktop app" in message
    assert "doppler run" in message


def test_config_prefers_the_opendj_specific_names() -> None:
    config = GoogleOAuthConfig.from_env(
        {
            "OPENDJ_GOOGLE_OAUTH_CLIENT_ID": "specific-id",
            "OPENDJ_GOOGLE_OAUTH_CLIENT_SECRET": "specific-secret",
            "GOOGLE_OAUTH_CLIENT_ID": "shared-id",
            "GOOGLE_OAUTH_CLIENT_SECRET": "shared-secret",
        }
    )
    assert config.client_id == "specific-id"


def test_blank_credentials_count_as_missing() -> None:
    with pytest.raises(AuthConfigError):
        GoogleOAuthConfig.from_env(
            {"GOOGLE_OAUTH_CLIENT_ID": "  ", "GOOGLE_OAUTH_CLIENT_SECRET": ""}
        )


def test_oauth_config_missing_env_returns_503_with_runbook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (*auth_mod.CLIENT_ID_ENV_NAMES, *auth_mod.CLIENT_SECRET_ENV_NAMES):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(HTTPException) as excinfo:
        auth_routes._oauth_config()
    assert excinfo.value.status_code == 503
    detail = excinfo.value.detail
    assert detail["code"] == "AUTH_NOT_CONFIGURED"
    message = detail["message"]
    assert "OPENDJ_GOOGLE_OAUTH_CLIENT_ID" in message
    assert "OPENDJ_GOOGLE_OAUTH_CLIENT_SECRET" in message
    assert "doppler run" in message
    assert "Desktop app" in message


def test_login_is_503_when_credentials_are_absent(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (*auth_mod.CLIENT_ID_ENV_NAMES, *auth_mod.CLIENT_SECRET_ENV_NAMES):
        monkeypatch.delenv(name, raising=False)
    response = client.post("/api/v1/auth/login", json={})
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["code"] == "AUTH_NOT_CONFIGURED"
    message = detail["message"]
    assert "OPENDJ_GOOGLE_OAUTH_CLIENT_ID" in message
    assert "OPENDJ_GOOGLE_OAUTH_CLIENT_SECRET" in message
    assert "doppler run" in message
    assert "Desktop app" in message


def test_login_returns_a_consent_url_when_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENDJ_GOOGLE_OAUTH_CLIENT_ID", FAKE_CONFIG.client_id)
    monkeypatch.setenv(
        "OPENDJ_GOOGLE_OAUTH_CLIENT_SECRET", FAKE_CONFIG.client_secret
    )
    body = client.post(
        "/api/v1/auth/login", json={"origin": "http://127.0.0.1:9418"}
    ).json()
    assert body["redirect_uri"] == "http://127.0.0.1:9418/api/v1/auth/callback"
    assert body["state"] in body["authorization_url"]


# ----- origin validation --------------------------------------------------


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example.com",
        "http://evil.example.com",
        "http://127.0.0.1.evil.com:9418",
    ],
)
def test_login_refuses_a_non_loopback_origin(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, origin: str
) -> None:
    monkeypatch.setenv("OPENDJ_GOOGLE_OAUTH_CLIENT_ID", FAKE_CONFIG.client_id)
    monkeypatch.setenv(
        "OPENDJ_GOOGLE_OAUTH_CLIENT_SECRET", FAKE_CONFIG.client_secret
    )
    response = client.post("/api/v1/auth/login", json={"origin": origin})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "AUTH_ORIGIN_INVALID"


# ----- id_token parsing ---------------------------------------------------
# Pure decoding of a token-endpoint payload. This is not a stand-in for a
# live sign-in; it only pins the claim mapping so a Google field rename
# fails here rather than silently producing a user with no email.


def _jwt_with_claims(claims: dict[str, object]) -> str:
    def segment(payload: dict[str, object]) -> str:
        raw = json.dumps(payload).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{segment({'alg': 'RS256'})}.{segment(claims)}.signature-not-checked"


def test_identity_maps_google_claims() -> None:
    identity = identity_from_token_response(
        {
            "access_token": "at",
            "refresh_token": "rt",
            "expires_in": 3599,
            "id_token": _jwt_with_claims(
                {
                    "sub": "1234567890",
                    "email": "maintainer",
                    "name": "Tamsin Quell",
                    "picture": "https://lh3.googleusercontent.com/a/pic",
                }
            ),
        }
    )
    assert identity.google_sub == "1234567890"
    assert identity.email == "maintainer"
    assert identity.avatar_url == "https://lh3.googleusercontent.com/a/pic"
    assert identity.refresh_token == "rt"


def test_identity_rejects_a_response_with_no_id_token() -> None:
    with pytest.raises(AuthFlowError):
        identity_from_token_response({"access_token": "at", "expires_in": 60})


def test_identity_rejects_claims_with_no_subject() -> None:
    with pytest.raises(AuthFlowError):
        identity_from_token_response(
            {
                "access_token": "at",
                "expires_in": 60,
                "id_token": _jwt_with_claims({"email": "a@b.c"}),
            }
        )

pytestmark = pytest.mark.rb_parity
