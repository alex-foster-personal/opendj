"""Google sign-in core for the webui daemon.

Authorization Code flow with PKCE against a Google "Desktop app" (installed)
OAuth client, which is what lets every worktree use its own loopback port
without registering a redirect URI per port. Scopes are ``openid email
profile`` and nothing else -- this is identity, not Workspace access.

Split of concerns:
  * :class:`GoogleOAuthConfig`  -- credentials, loaded from the environment.
  * :class:`PendingLoginStore`  -- short-lived CSRF ``state`` + PKCE verifier.
  * :class:`SessionStore`       -- sqlite persistence of users and sessions.
  * :func:`exchange_code`       -- the one network call, to Google's token
                                  endpoint.

The HTTP call uses :mod:`urllib.request` on purpose: ``httpx`` is a dev-only
dependency in pyproject.toml (it ships for Starlette's TestClient), so
reaching for it here would put a test dependency on the production path.

No fallbacks. A missing client id, an expired ``state``, or a token endpoint
that answers anything other than 200 raises -- the caller turns that into an
HTTP error the UI shows. Nothing here invents a signed-in state.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from apps.shared.state import db as state_db

# ----- constants ---------------------------------------------------------

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"

SCOPES: tuple[str, ...] = ("openid", "email", "profile")

SESSION_COOKIE_NAME = "opendj_session"
SESSION_TTL = timedelta(days=30)
PENDING_LOGIN_TTL_SECONDS = 600
CALLBACK_PATH = "/api/v1/auth/callback"

# Precedence is explicit, not a hidden default: an openDJ-specific client
# wins if one is ever provisioned, otherwise the shared personal client in
# Doppler (project ``general``, config ``dev_personal``) is used.
CLIENT_ID_ENV_NAMES: tuple[str, ...] = (
    "OPENDJ_GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_ID",
)
CLIENT_SECRET_ENV_NAMES: tuple[str, ...] = (
    "OPENDJ_GOOGLE_OAUTH_CLIENT_SECRET",
    "GOOGLE_OAUTH_CLIENT_SECRET",
)

_MISSING_CREDENTIALS_RUNBOOK = """\
Google sign-in is not configured. The daemon needs a Google OAuth client id
and secret in its environment; it will not guess and it will not run a
degraded sign-in.

Expected environment variables (first match wins):
  client id      {id_names}
  client secret  {secret_names}

Start the daemon through Doppler so they are injected:
  doppler run --project general --config dev_personal -- \\
    uv run --no-sync python -m apps.webui.server --host 127.0.0.1

If no OAuth client exists yet, create one (a human must do this in a
browser; it cannot be automated):
  1. Google Cloud console > APIs & Services > OAuth consent screen.
     Publish the app to Production. A consent screen left in Testing
     expires every refresh token after 7 days, so sign-in silently stops
     working a week later.
  2. APIs & Services > Credentials > Create credentials >
     OAuth client ID > Application type: Desktop app.
     Desktop clients accept any http://127.0.0.1:<port>/... loopback
     redirect, which is what lets each worktree use its own port. A "Web
     application" client would need every port registered by hand.
  3. Save the new id and secret to Doppler:
       doppler secrets set OPENDJ_GOOGLE_OAUTH_CLIENT_ID=... \\
         --project general --config dev_personal
       doppler secrets set OPENDJ_GOOGLE_OAUTH_CLIENT_SECRET=... \\
         --project general --config dev_personal
"""


class AuthConfigError(RuntimeError):
    """Raised when the Google OAuth client is not configured."""


class AuthFlowError(RuntimeError):
    """Raised when a sign-in attempt cannot be completed."""


# ----- config ------------------------------------------------------------


@dataclass(frozen=True)
class GoogleOAuthConfig:
    """Credentials for the Google OAuth client backing webui sign-in."""

    client_id: str
    client_secret: str

    @classmethod
    def from_env(cls, env: dict[str, str]) -> GoogleOAuthConfig:
        """Build from ``env``; raise :class:`AuthConfigError` if unset.

        Fails fast and loudly: the message is the full provisioning runbook
        so an operator never has to guess which secret is missing.
        """
        client_id = _first_present(env, CLIENT_ID_ENV_NAMES)
        client_secret = _first_present(env, CLIENT_SECRET_ENV_NAMES)
        if not client_id or not client_secret:
            raise AuthConfigError(
                _MISSING_CREDENTIALS_RUNBOOK.format(
                    id_names=" or ".join(CLIENT_ID_ENV_NAMES),
                    secret_names=" or ".join(CLIENT_SECRET_ENV_NAMES),
                )
            )
        return cls(client_id=client_id, client_secret=client_secret)


def _first_present(env: dict[str, str], names: tuple[str, ...]) -> str | None:
    for name in names:
        value = env.get(name, "").strip()
        if value:
            return value
    return None


# ----- pending logins (CSRF state + PKCE) --------------------------------


@dataclass(frozen=True)
class PendingLogin:
    """One in-flight authorization request."""

    state: str
    code_verifier: str
    redirect_uri: str
    created_at: float


class PendingLoginStore:
    """In-memory ``state`` -> :class:`PendingLogin` map with a TTL.

    Deliberately not persisted. A pending login is meaningful for the few
    seconds between opening Google's consent screen and being redirected
    back; a daemon restart in that window should invalidate it rather than
    resurrect it. Entries are single-use, so a replayed ``state`` fails.
    """

    def __init__(self, ttl_seconds: int = PENDING_LOGIN_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._entries: dict[str, PendingLogin] = {}

    def create(self, redirect_uri: str) -> PendingLogin:
        self._purge_expired()
        pending = PendingLogin(
            state=secrets.token_urlsafe(32),
            code_verifier=secrets.token_urlsafe(64),
            redirect_uri=redirect_uri,
            created_at=time.monotonic(),
        )
        self._entries[pending.state] = pending
        return pending

    def consume(self, state: str) -> PendingLogin:
        """Pop and return the pending login for ``state``.

        Raises :class:`AuthFlowError` for an unknown, replayed or expired
        state -- all three are indistinguishable to an attacker on purpose.
        """
        self._purge_expired()
        pending = self._entries.pop(state, None)
        if pending is None:
            raise AuthFlowError(
                "unknown or expired sign-in state; start the sign-in again"
            )
        return pending

    def _purge_expired(self) -> None:
        cutoff = time.monotonic() - self._ttl
        for state, pending in list(self._entries.items()):
            if pending.created_at < cutoff:
                del self._entries[state]

    def __len__(self) -> int:
        return len(self._entries)


# ----- PKCE + authorization URL ------------------------------------------


def code_challenge_for(code_verifier: str) -> str:
    """S256 PKCE challenge for ``code_verifier`` (RFC 7636)."""
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def build_authorization_url(
    config: GoogleOAuthConfig, pending: PendingLogin
) -> str:
    """Google consent URL for ``pending``.

    ``access_type=offline`` plus ``prompt=consent`` guarantees a refresh
    token on every sign-in. Without ``prompt=consent`` Google returns one
    only on the very first grant, so a re-login after a DB wipe would leave
    the session unable to refresh.
    """
    params = {
        "client_id": config.client_id,
        "redirect_uri": pending.redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "state": pending.state,
        "code_challenge": code_challenge_for(pending.code_verifier),
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"{AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}"


def callback_url_for_origin(origin: str) -> str:
    """Loopback redirect URI for a browser served from ``origin``."""
    return f"{origin.rstrip('/')}{CALLBACK_PATH}"


# ----- token exchange ----------------------------------------------------


@dataclass(frozen=True)
class GoogleIdentity:
    """The identity claims we persist, straight from Google's id_token."""

    google_sub: str
    email: str
    name: str | None
    avatar_url: str | None
    refresh_token: str | None
    access_token: str
    access_expires_at: str


def exchange_code(
    config: GoogleOAuthConfig,
    pending: PendingLogin,
    code: str,
    *,
    timeout: float = 30.0,
) -> GoogleIdentity:
    """Trade ``code`` for tokens at Google's token endpoint.

    Raises :class:`AuthFlowError` on any non-200 response, including
    Google's own error body, so the caller never sees a half-built
    identity.
    """
    payload = urllib.parse.urlencode(
        {
            "client_id": config.client_id,
            "client_secret": config.client_secret,
            "code": code,
            "code_verifier": pending.code_verifier,
            "grant_type": "authorization_code",
            "redirect_uri": pending.redirect_uri,
        }
    ).encode("ascii")
    request = urllib.request.Request(
        TOKEN_ENDPOINT,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise AuthFlowError(
            f"Google token endpoint returned {exc.code}: {detail}"
        ) from exc
    except urllib.error.URLError as exc:
        raise AuthFlowError(
            f"could not reach Google token endpoint: {exc.reason}"
        ) from exc
    return identity_from_token_response(body)


def identity_from_token_response(body: dict[str, Any]) -> GoogleIdentity:
    """Build a :class:`GoogleIdentity` from a token endpoint response."""
    id_token = body.get("id_token")
    if not id_token:
        raise AuthFlowError(
            "Google token response carried no id_token; the 'openid' scope "
            "was not granted"
        )
    claims = decode_id_token_claims(id_token)
    google_sub = claims.get("sub")
    email = claims.get("email")
    if not google_sub or not email:
        raise AuthFlowError(
            "Google id_token is missing the 'sub' or 'email' claim"
        )
    access_token = body.get("access_token")
    if not access_token:
        raise AuthFlowError("Google token response carried no access_token")
    expires_in = int(body.get("expires_in", 0))
    access_expires_at = _iso(
        datetime.now(UTC) + timedelta(seconds=expires_in)
    )
    return GoogleIdentity(
        google_sub=str(google_sub),
        email=str(email),
        name=claims.get("name"),
        avatar_url=claims.get("picture"),
        refresh_token=body.get("refresh_token"),
        access_token=str(access_token),
        access_expires_at=access_expires_at,
    )


def decode_id_token_claims(id_token: str) -> dict[str, Any]:
    """Return the claim set of ``id_token`` without verifying its signature.

    Signature verification is intentionally skipped, and this is Google's
    own documented guidance for this case: the token arrived over TLS in a
    direct server-to-server response from Google's token endpoint, so there
    is no third party who could have substituted it. Do NOT reuse this
    helper on an id_token that came from a browser or any other untrusted
    hop -- that path needs full JWKS verification.
    """
    segments = id_token.split(".")
    if len(segments) != 3:
        raise AuthFlowError("malformed id_token: expected three JWT segments")
    payload = segments[1]
    padding = "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload + padding)
        claims = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise AuthFlowError(f"could not decode id_token claims: {exc}") from exc
    if not isinstance(claims, dict):
        raise AuthFlowError("id_token claims were not a JSON object")
    return claims


# ----- persistence -------------------------------------------------------


@dataclass(frozen=True)
class SessionUser:
    """A signed-in user as the API exposes them. No tokens here, ever."""

    google_sub: str
    email: str
    name: str | None
    avatar_url: str | None
    created_at: str


def hash_session_token(token: str) -> str:
    """sha256 of the cookie value; only the hash reaches the database."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


class SessionStore:
    """sqlite persistence for ``users`` and ``auth_sessions``.

    A connection is opened per call rather than held: FastAPI runs sync
    endpoints on a thread pool, and a per-call connection avoids sharing a
    handle across threads. ``open_rw`` applies migrations and sets
    ``PRAGMA foreign_keys = ON``, so the auth_sessions -> users FK is
    enforced on every write.
    """

    def __init__(self, state_db_path: str | Path) -> None:
        self._path = Path(state_db_path)

    @property
    def path(self) -> Path:
        return self._path

    def _connect(self) -> sqlite3.Connection:
        return state_db.open_rw(self._path, check_same_thread=False)

    def sign_in(self, identity: GoogleIdentity) -> str:
        """Upsert the user, open a session, return the raw session token.

        The raw token is returned exactly once, to be planted in the
        browser cookie. Only its hash is stored.
        """
        now = datetime.now(UTC)
        now_iso = _iso(now)
        token = secrets.token_urlsafe(32)
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            conn.execute(
                """
                INSERT INTO users(
                    google_sub, email, name, avatar_url, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(google_sub) DO UPDATE SET
                    email      = excluded.email,
                    name       = excluded.name,
                    avatar_url = excluded.avatar_url,
                    updated_at = excluded.updated_at
                """,
                (
                    identity.google_sub, identity.email, identity.name,
                    identity.avatar_url, now_iso, now_iso,
                ),
            )
            conn.execute(
                """
                INSERT INTO auth_sessions(
                    session_token_sha256, google_sub, refresh_token,
                    access_token, access_expires_at, created_at,
                    last_seen_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    hash_session_token(token), identity.google_sub,
                    identity.refresh_token, identity.access_token,
                    identity.access_expires_at, now_iso, now_iso,
                    _iso(now + SESSION_TTL),
                ),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        return token

    def resolve(self, token: str) -> SessionUser | None:
        """Return the user behind ``token``, or None if it is not valid.

        Touches ``last_seen_at`` on success. An expired row is deleted
        rather than merely ignored, so the table does not accumulate dead
        sessions.
        """
        token_hash = hash_session_token(token)
        now = datetime.now(UTC)
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT u.google_sub, u.email, u.name, u.avatar_url,
                       u.created_at, s.expires_at
                FROM auth_sessions AS s
                JOIN users AS u ON u.google_sub = s.google_sub
                WHERE s.session_token_sha256 = ?
                """,
                (token_hash,),
            ).fetchone()
            if row is None:
                return None
            if datetime.fromisoformat(row[5]) <= now:
                conn.execute(
                    "DELETE FROM auth_sessions WHERE session_token_sha256 = ?",
                    (token_hash,),
                )
                return None
            conn.execute(
                "UPDATE auth_sessions SET last_seen_at = ? "
                "WHERE session_token_sha256 = ?",
                (_iso(now), token_hash),
            )
            return SessionUser(
                google_sub=row[0], email=row[1], name=row[2],
                avatar_url=row[3], created_at=row[4],
            )
        finally:
            conn.close()

    def sign_out(self, token: str) -> bool:
        """Delete the session for ``token``; True if one was removed."""
        conn = self._connect()
        try:
            cursor = conn.execute(
                "DELETE FROM auth_sessions WHERE session_token_sha256 = ?",
                (hash_session_token(token),),
            )
            return cursor.rowcount > 0
        finally:
            conn.close()

    def delete_user(self, google_sub: str) -> bool:
        """Erase the user row and, by cascade, every session it owns.

        The deletion half of the privacy promise at https://open-dj.com/privacy
        (ACCT-03): signing out drops one session, this drops the account. The
        ``auth_sessions -> users`` FK is ``ON DELETE CASCADE`` and ``open_rw``
        sets ``PRAGMA foreign_keys = ON``, so the Google refresh and access
        tokens held alongside those sessions go with it -- there is no second
        statement that could be forgotten.

        Returns True if a row was removed, False if there was nothing to
        remove. Idempotent on purpose: deleting data that is already gone is
        the outcome the caller asked for, not an error.
        """
        conn = self._connect()
        try:
            cursor = conn.execute(
                "DELETE FROM users WHERE google_sub = ?", (google_sub,)
            )
            return cursor.rowcount > 0
        finally:
            conn.close()


__all__ = [
    "AUTH_ENDPOINT",
    "CALLBACK_PATH",
    "CLIENT_ID_ENV_NAMES",
    "CLIENT_SECRET_ENV_NAMES",
    "REVOKE_ENDPOINT",
    "SCOPES",
    "SESSION_COOKIE_NAME",
    "SESSION_TTL",
    "TOKEN_ENDPOINT",
    "AuthConfigError",
    "AuthFlowError",
    "GoogleIdentity",
    "GoogleOAuthConfig",
    "PendingLogin",
    "PendingLoginStore",
    "SessionStore",
    "SessionUser",
    "build_authorization_url",
    "callback_url_for_origin",
    "code_challenge_for",
    "decode_id_token_claims",
    "exchange_code",
    "hash_session_token",
    "identity_from_token_response",
]
