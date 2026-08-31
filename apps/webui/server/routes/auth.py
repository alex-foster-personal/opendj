"""Google sign-in endpoints for the webui daemon.

Full agent-native parity with the user bauble in the UI -- every step the
browser takes has an HTTP equivalent an agent can drive:

  POST /api/v1/auth/login     -> {authorization_url, state, redirect_uri}
  GET  /api/v1/auth/callback  -> Google's loopback redirect target; plants
                                 the session cookie and bounces to the SPA
  GET  /api/v1/auth/me        -> the signed-in user, or 401
  POST /api/v1/auth/logout    -> drops the session and clears the cookie

Cookie model: the browser holds an opaque token in an httpOnly cookie, the
daemon holds its sha256 alongside the Google refresh token. The browser
never sees a Google token, and the session outlives both a daemon restart
and a browser restart because it lives in ``data/state/state.db``.
"""
from __future__ import annotations

import os
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict

from apps.webui.server.auth import (
    SESSION_COOKIE_NAME,
    SESSION_TTL,
    AuthConfigError,
    AuthFlowError,
    GoogleOAuthConfig,
    PendingLoginStore,
    SessionStore,
    SessionUser,
    build_authorization_url,
    callback_url_for_origin,
    exchange_code,
)

router = APIRouter(prefix="/auth", tags=["auth"])

_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})


# ----- request-scoped singletons -----------------------------------------


def _pending_logins(request: Request) -> PendingLoginStore:
    """The app-wide pending-login store, created on first use."""
    store = getattr(request.app.state, "pending_logins", None)
    if store is None:
        store = PendingLoginStore()
        request.app.state.pending_logins = store
    return store


def _session_store(request: Request) -> SessionStore:
    """The app-wide session store over this app's configured state DB."""
    store = getattr(request.app.state, "session_store", None)
    if store is None:
        store = SessionStore(request.app.state.state_db_path)
        request.app.state.session_store = store
    return store


def _oauth_config() -> GoogleOAuthConfig:
    """Load Google credentials, or 503 with the provisioning runbook."""
    try:
        return GoogleOAuthConfig.from_env(dict(os.environ))
    except AuthConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "AUTH_NOT_CONFIGURED", "message": str(exc)},
        ) from exc


# ----- origin resolution --------------------------------------------------


def _validate_loopback_origin(origin: str) -> str:
    """Return ``origin`` normalised, or 422 if it is not http loopback.

    Guards two things at once: an open redirect out of the callback, and a
    session cookie being planted on an origin the daemon does not serve.
    """
    parsed = urlparse(origin)
    if parsed.scheme != "http" or parsed.hostname not in _LOOPBACK_HOSTS:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "AUTH_ORIGIN_INVALID",
                "message": (
                    f"sign-in origin must be http loopback, got {origin!r}. "
                    "The webui is a local-first daemon; it will not redirect "
                    "a sign-in anywhere else."
                ),
            },
        )
    port = f":{parsed.port}" if parsed.port else ""
    return f"http://{parsed.hostname}{port}"


def _resolve_origin(request: Request, requested: str | None) -> str:
    """Pick the browser origin this sign-in belongs to.

    Precedence, explicit rather than implied:
      1. the ``origin`` field in the request body (an agent driving the
         flow with curl, which sends no Origin header)
      2. the Origin header (the SPA's own fetch)
      3. this daemon's own loopback origin (the SPA served from the built
         bundle, same port as the API)
    """
    if requested:
        return _validate_loopback_origin(requested)
    header_origin = request.headers.get("origin")
    if header_origin:
        return _validate_loopback_origin(header_origin)
    port = getattr(request.app.state, "port", None)
    if port is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "AUTH_ORIGIN_UNRESOLVED",
                "message": (
                    "cannot determine the browser origin for this sign-in: "
                    "no 'origin' in the body, no Origin header, and the "
                    "daemon has no configured port. Pass "
                    '{"origin": "http://127.0.0.1:<frontend-port>"}.'
                ),
            },
        )
    return f"http://127.0.0.1:{port}"


# ----- models -------------------------------------------------------------


class LoginIn(BaseModel):
    model_config = ConfigDict(frozen=True)

    origin: str | None = None
    """Loopback origin the browser is on, e.g. http://127.0.0.1:9418.
    Omit when calling from the SPA -- the Origin header covers it."""


class LoginOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    authorization_url: str
    state: str
    redirect_uri: str


class MeOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    google_sub: str
    email: str
    name: str | None
    avatar_url: str | None
    created_at: str

    @classmethod
    def of(cls, user: SessionUser) -> MeOut:
        return cls(
            google_sub=user.google_sub, email=user.email, name=user.name,
            avatar_url=user.avatar_url, created_at=user.created_at,
        )


# ----- endpoints ----------------------------------------------------------


@router.post("/login", response_model=LoginOut)
def start_login(body: LoginIn, request: Request) -> LoginOut:
    """Begin sign-in: mint CSRF state + PKCE and return the consent URL.

    The caller (browser or agent) is responsible for actually visiting
    ``authorization_url``. Nothing is persisted until the callback lands.
    """
    config = _oauth_config()
    origin = _resolve_origin(request, body.origin)
    pending = _pending_logins(request).create(callback_url_for_origin(origin))
    return LoginOut(
        authorization_url=build_authorization_url(config, pending),
        state=pending.state,
        redirect_uri=pending.redirect_uri,
    )


@router.get("/callback", include_in_schema=True)
def finish_login(
    request: Request,
    state: str = "",
    code: str = "",
    error: str = "",
) -> RedirectResponse:
    """Google's loopback redirect target. Plants the cookie, returns to the SPA."""
    if error:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "AUTH_DENIED",
                "message": f"Google refused the sign-in: {error}",
            },
        )
    if not state or not code:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "AUTH_CALLBACK_INCOMPLETE",
                "message": (
                    "callback needs both 'state' and 'code'; this URL is "
                    "Google's redirect target, not a page to open by hand"
                ),
            },
        )
    config = _oauth_config()
    try:
        pending = _pending_logins(request).consume(state)
        identity = exchange_code(config, pending, code)
    except AuthFlowError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "AUTH_EXCHANGE_FAILED", "message": str(exc)},
        ) from exc

    token = _session_store(request).sign_in(identity)
    spa_origin = pending.redirect_uri.removesuffix("/api/v1/auth/callback")
    response = RedirectResponse(
        url=f"{spa_origin}/", status_code=status.HTTP_303_SEE_OTHER,
    )
    _set_session_cookie(response, token)
    return response


@router.get("/me", response_model=MeOut)
def whoami(request: Request) -> MeOut:
    """The signed-in user, or 401. The bauble polls this on mount."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    user = _session_store(request).resolve(token) if token else None
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "AUTH_REQUIRED", "message": "not signed in"},
        )
    return MeOut.of(user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request) -> Response:
    """Drop the session server-side and clear the cookie.

    Idempotent: signing out when already signed out is a 204, not an error.
    """
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        _session_store(request).sign_out(token)
    out = Response(status_code=status.HTTP_204_NO_CONTENT)
    out.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return out


def _set_session_cookie(response: Response, token: str) -> None:
    """Plant the session cookie.

    ``secure=False`` because the daemon is http on loopback by design (see
    the bind-host warning in app.py); a Secure cookie would simply never be
    stored and sign-in would appear to succeed and then do nothing.
    ``samesite='lax'`` is required, not incidental: the cookie is set during
    a top-level cross-site redirect back from accounts.google.com, which
    'strict' would drop.
    """
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=False,
        path="/",
    )


__all__ = ["router"]
