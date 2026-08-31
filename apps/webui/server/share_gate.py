"""Restricted share audience in front of the otherwise authless daemon.

Friends hit a public hostname (Cloudflare Tunnel + Access). You keep
loopback / Tailscale. This module does not invent users.

When ``MUSIC_DJ_SHARE_HOST`` matches the request Host (or X-Forwarded-Host):

- Cloudflare Access identity headers must be present (the default), or a
  legacy ``MUSIC_DJ_SHARE_TOKEN`` must be presented when explicitly selected
- writes are refused when ``MUSIC_DJ_SHARE_READ_ONLY=1`` (the default)

Loopback and any other Host stay full-access so agents and ``just run``
keep working. Cloudflare Access is still the outer door; this is the
app-level belt so a mis-published port cannot write the library.
"""
from __future__ import annotations

import hmac
import os
from collections.abc import Mapping
from dataclasses import dataclass

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

SHARE_HOST_ENV = "MUSIC_DJ_SHARE_HOST"
SHARE_AUTH_ENV = "MUSIC_DJ_SHARE_AUTH"
SHARE_TOKEN_ENV = "MUSIC_DJ_SHARE_TOKEN"
SHARE_READ_ONLY_ENV = "MUSIC_DJ_SHARE_READ_ONLY"
SHARE_COOKIE = "mdj_share"
AUTH_CLOUDFLARE_ACCESS = "cloudflare-access"
AUTH_TOKEN = "token"
ACCESS_EMAIL_HEADER = "cf-access-authenticated-user-email"
ACCESS_JWT_HEADER = "cf-access-jwt-assertion"
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
EXEMPT_SUFFIXES = (
    "/health",
    "/api/v1/health",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/api/v1/share/session",
)


@dataclass(frozen=True)
class ShareConfig:
    host: str = ""
    auth: str = AUTH_CLOUDFLARE_ACCESS
    token: str = ""
    read_only: bool = True

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] = os.environ) -> ShareConfig:
        host = environ.get(SHARE_HOST_ENV, "").strip().lower()
        token = environ.get(SHARE_TOKEN_ENV, "").strip()
        default_auth = AUTH_TOKEN if token else AUTH_CLOUDFLARE_ACCESS
        auth = environ.get(SHARE_AUTH_ENV, default_auth).strip().lower()
        raw_read_only = environ.get(SHARE_READ_ONLY_ENV, "1").strip().lower()
        config = cls(
            host=host,
            auth=auth,
            token=token,
            read_only=raw_read_only not in {"0", "false", "no", "off"},
        )
        config.validate()
        return config

    def validate(self) -> None:
        if not self.host:
            return
        if self.auth not in {AUTH_CLOUDFLARE_ACCESS, AUTH_TOKEN}:
            raise ValueError(
                f"{SHARE_AUTH_ENV} must be {AUTH_CLOUDFLARE_ACCESS!r} or "
                f"{AUTH_TOKEN!r}"
            )
        if self.auth == AUTH_TOKEN and not self.token:
            raise ValueError(
                f"{SHARE_TOKEN_ENV} is required when {SHARE_AUTH_ENV}={AUTH_TOKEN}"
            )


@dataclass(frozen=True)
class ShareDecision:
    audience: str  # local | share
    read_only: bool
    authorized: bool
    error: str | None = None


def configured_share(request: Request) -> ShareConfig:
    config = getattr(request.app.state, "share_config", None)
    return config if isinstance(config, ShareConfig) else ShareConfig.from_environ()


def request_host(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-host", "")
    raw = (forwarded.split(",", 1)[0] or request.headers.get("host") or "")
    host = raw.strip().lower()
    if ":" in host and not host.startswith("["):
        host = host.rsplit(":", 1)[0]
    return host


def presented_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    cookie = request.cookies.get(SHARE_COOKIE, "")
    if cookie:
        return cookie
    return (request.query_params.get("share") or "").strip()


def decide(request: Request, config: ShareConfig | None = None) -> ShareDecision:
    configured = config or configured_share(request)
    host = request_host(request)
    if not configured.host or host != configured.host:
        return ShareDecision(audience="local", read_only=False, authorized=True)
    if configured.auth == AUTH_CLOUDFLARE_ACCESS:
        email = request.headers.get(ACCESS_EMAIL_HEADER, "").strip()
        assertion = request.headers.get(ACCESS_JWT_HEADER, "").strip()
        if not email or not assertion:
            return ShareDecision(
                audience="share",
                read_only=True,
                authorized=False,
                error="Cloudflare Access identity is missing",
            )
    elif not hmac.compare_digest(presented_token(request), configured.token):
        return ShareDecision(
            audience="share",
            read_only=True,
            authorized=False,
            error="share token missing or wrong",
        )
    return ShareDecision(
        audience="share",
        read_only=configured.read_only,
        authorized=True,
    )


def is_exempt(path: str) -> bool:
    return any(path == suffix or path.endswith(suffix) for suffix in EXEMPT_SUFFIXES)


async def share_gate_middleware(request: Request, call_next) -> Response:
    config = configured_share(request)
    decision = decide(request, config)
    request.state.share_audience = decision.audience
    request.state.share_read_only = decision.read_only
    # The token session route and legacy health/docs exemptions predate Access.
    # Access mode guards every public-host request; cloudflared independently
    # verifies the JWT signature and audience before forwarding to loopback.
    if config.auth == AUTH_TOKEN and is_exempt(request.url.path):
        return await call_next(request)
    if not decision.authorized:
        return JSONResponse(
            {
                "code": "SHARE_UNAUTHORIZED",
                "message": decision.error or "share token required",
            },
            status_code=401,
        )
    if decision.read_only and request.method in WRITE_METHODS:
        return JSONResponse(
            {
                "code": "SHARE_READ_ONLY",
                "message": "this share host is read-only",
            },
            status_code=403,
        )
    return await call_next(request)
