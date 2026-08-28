"""Restricted share audience in front of the otherwise authless daemon.

Friends hit a public hostname (Cloudflare Tunnel + Access). You keep
loopback / Tailscale. This module does not invent users.

When ``MUSIC_DJ_SHARE_HOST`` matches the request Host (or X-Forwarded-Host):

- optional ``MUSIC_DJ_SHARE_TOKEN`` must be presented
- writes are refused when ``MUSIC_DJ_SHARE_READ_ONLY=1`` (the default)

Loopback and any other Host stay full-access so agents and ``just run``
keep working. Cloudflare Access is still the outer door; this is the
app-level belt so a mis-published port cannot write the library.
"""
from __future__ import annotations

import hmac
import os
from dataclasses import dataclass
from typing import Optional

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

SHARE_HOST_ENV = "MUSIC_DJ_SHARE_HOST"
SHARE_TOKEN_ENV = "MUSIC_DJ_SHARE_TOKEN"
SHARE_READ_ONLY_ENV = "MUSIC_DJ_SHARE_READ_ONLY"
SHARE_COOKIE = "mdj_share"
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
class ShareDecision:
    audience: str  # local | share
    read_only: bool
    authorized: bool
    error: Optional[str] = None


def share_host() -> str:
    return os.environ.get(SHARE_HOST_ENV, "").strip().lower()


def share_token() -> str:
    return os.environ.get(SHARE_TOKEN_ENV, "").strip()


def share_read_only() -> bool:
    raw = os.environ.get(SHARE_READ_ONLY_ENV, "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


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


def decide(request: Request) -> ShareDecision:
    configured = share_host()
    host = request_host(request)
    if not configured or host != configured:
        return ShareDecision(audience="local", read_only=False, authorized=True)
    token = share_token()
    got = presented_token(request)
    if token and not hmac.compare_digest(got, token):
        return ShareDecision(
            audience="share",
            read_only=True,
            authorized=False,
            error="share token missing or wrong",
        )
    return ShareDecision(
        audience="share",
        read_only=share_read_only(),
        authorized=True,
    )


def is_exempt(path: str) -> bool:
    return any(path == suffix or path.endswith(suffix) for suffix in EXEMPT_SUFFIXES)


async def share_gate_middleware(request: Request, call_next) -> Response:
    decision = decide(request)
    request.state.share_audience = decision.audience
    request.state.share_read_only = decision.read_only
    if is_exempt(request.url.path):
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
