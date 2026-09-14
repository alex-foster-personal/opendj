"""Global host allowlist and mutating-origin guard for the authless daemon.

Closes DNS-rebinding and cross-site simple-POST writes while preserving agent
and CLI parity: requests without an ``Origin`` header keep working.
"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from apps.shared.sync_bind_guard import is_loopback_host
from apps.webui.port_config import WEBUI_ENV_FILE, _read_dotenv, _resolved_raw_value

ALLOWED_HOSTS_ENV = "MUSIC_DJ_ALLOWED_HOSTS"
SHARE_ORIGIN_ENV = "MUSIC_DJ_SHARE_ORIGIN"
HOSTNAME_RE = re.compile(
    r"^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*$"
)
_LOOPBACK_HOSTNAMES: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})
_MUTATING_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_BENCH_ORIGIN_REGEX = r"^http://(localhost|127\.0\.0\.1):87\d\d$"
_E2E_FRONTEND_PORTS: tuple[int, ...] = (5273, 5275)


class RequestGuardBindRefused(RuntimeError):
    """Refused to bind on a non-loopback address without an explicit host allowlist."""


@dataclass(frozen=True)
class TrustedOrigins:
    origins: tuple[str, ...]
    origin_regex: str | None


def request_host(request: Request) -> str:
    """Bare hostname from Host / X-Forwarded-Host (first value, port stripped)."""
    forwarded = request.headers.get("x-forwarded-host", "")
    raw = (forwarded.split(",", 1)[0] or request.headers.get("host") or "")
    host = raw.strip().lower()
    if host.startswith("["):
        end = host.find("]")
        if end != -1:
            host = host[1:end]
        elif ":" in host:
            host = host[1:].rsplit(":", 1)[0]
    elif ":" in host:
        host = host.rsplit(":", 1)[0]
    return host


def parse_allowed_hosts(
    raw_value: str | None,
    *,
    env_name: str = ALLOWED_HOSTS_ENV,
) -> list[str]:
    """Bare hostnames from ``MUSIC_DJ_ALLOWED_HOSTS``. Unset means loopback only."""
    if raw_value is None or raw_value.strip() == "":
        return []
    hosts = [part.strip() for part in raw_value.split(",") if part.strip()]
    if not hosts:
        raise ValueError(f"{env_name} was set but named no hostname")
    for host in hosts:
        if not HOSTNAME_RE.fullmatch(host):
            raise ValueError(
                f"{env_name} must list bare hostnames, got {host!r}"
            )
    return hosts


def resolve_allowed_hosts_from_environ(
    environ: Mapping[str, str] | None = None,
    *,
    dotenv_path: Path = WEBUI_ENV_FILE,
) -> list[str]:
    effective = os.environ if environ is None else environ
    raw = _resolved_raw_value(
        ALLOWED_HOSTS_ENV, effective, _read_dotenv(dotenv_path)
    )
    return parse_allowed_hosts(raw)


def build_allowed_hostnames(
    share_config,
    *,
    environ: Mapping[str, str] | None = None,
) -> frozenset[str]:
    hostnames = set(_LOOPBACK_HOSTNAMES)
    if share_config.host:
        hostnames.add(share_config.host)
    for host in resolve_allowed_hosts_from_environ(environ):
        hostnames.add(host.lower())
    return frozenset(hostnames)


def build_trusted_origins(
    *,
    frontend_port: int | None,
    backend_port: int | None,
    share_config,
    environ: Mapping[str, str] | None = None,
) -> TrustedOrigins:
    effective = os.environ if environ is None else environ
    origins: list[str] = []
    if frontend_port is not None:
        origins.extend(
            [
                f"http://localhost:{frontend_port}",
                f"http://127.0.0.1:{frontend_port}",
            ]
        )
    if backend_port is not None:
        origins.extend(
            [
                f"http://localhost:{backend_port}",
                f"http://127.0.0.1:{backend_port}",
                f"http://[::1]:{backend_port}",
            ]
        )
    share_origin = effective.get(SHARE_ORIGIN_ENV, "").strip()
    if not share_origin and share_config.host:
        share_origin = f"https://{share_config.host}"
    if share_origin:
        origins.append(share_origin)
    for port in _E2E_FRONTEND_PORTS:
        origins.extend(
            [
                f"http://localhost:{port}",
                f"http://127.0.0.1:{port}",
            ]
        )
    return TrustedOrigins(tuple(dict.fromkeys(origins)), _BENCH_ORIGIN_REGEX)


def install_request_guard(
    app,
    *,
    frontend_port: int | None,
    backend_port: int | None,
    enable_cors: bool,
    environ: Mapping[str, str] | None = None,
) -> None:
    share_config = app.state.share_config
    app.state.allowed_hostnames = build_allowed_hostnames(
        share_config, environ=environ
    )
    trusted = build_trusted_origins(
        frontend_port=frontend_port,
        backend_port=backend_port,
        share_config=share_config,
        environ=environ,
    )
    app.state.trusted_origins = trusted.origins
    app.state.trusted_origin_regex = trusted.origin_regex
    app.state.origin_guard_enabled = enable_cors


def assert_request_guard_bind_allowed(
    bind_host: str,
    environ: Mapping[str, str] | None = None,
) -> None:
    if is_loopback_host(bind_host):
        return
    allowed = resolve_allowed_hosts_from_environ(environ)
    if allowed:
        return
    raise RequestGuardBindRefused(
        f"refusing to bind on non-loopback {bind_host!r} without "
        f"{ALLOWED_HOSTS_ENV}: list the public hostname(s) the reverse "
        "proxy will present in Host (comma-separated bare names)."
    )


def _allowed_hostnames(request: Request) -> frozenset[str]:
    hostnames = getattr(request.app.state, "allowed_hostnames", None)
    if isinstance(hostnames, frozenset):
        return hostnames
    from apps.webui.server.share_gate import ShareConfig

    return build_allowed_hostnames(ShareConfig.from_environ())


def _origin_allowed(request: Request, origin: str) -> bool:
    trusted = getattr(request.app.state, "trusted_origins", ())
    regex = getattr(request.app.state, "trusted_origin_regex", None)
    if origin in trusted:
        return True
    if regex and re.fullmatch(regex, origin):
        return True
    return False


async def host_allowlist_middleware(request: Request, call_next) -> Response:
    hostname = request_host(request)
    if hostname not in _allowed_hostnames(request):
        return JSONResponse(
            {
                "code": "HOST_NOT_ALLOWED",
                "message": f"host {hostname!r} is not in the configured allowlist",
            },
            status_code=403,
        )
    return await call_next(request)


async def origin_guard_middleware(request: Request, call_next) -> Response:
    if not getattr(request.app.state, "origin_guard_enabled", False):
        return await call_next(request)
    if request.method not in _MUTATING_METHODS:
        return await call_next(request)
    origin = request.headers.get("origin")
    if not origin:
        return await call_next(request)
    if _origin_allowed(request, origin.strip()):
        return await call_next(request)
    return JSONResponse(
        {
            "code": "ORIGIN_NOT_ALLOWED",
            "message": f"origin {origin!r} is not in the trusted browser allowlist",
        },
        status_code=403,
    )


__all__ = [
    "ALLOWED_HOSTS_ENV",
    "RequestGuardBindRefused",
    "TrustedOrigins",
    "assert_request_guard_bind_allowed",
    "build_allowed_hostnames",
    "build_trusted_origins",
    "host_allowlist_middleware",
    "install_request_guard",
    "origin_guard_middleware",
    "parse_allowed_hosts",
    "request_host",
    "resolve_allowed_hosts_from_environ",
]
