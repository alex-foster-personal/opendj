"""Remote (streamable-HTTP) serving for the ``dispatch`` MCP server.

Topology, matching `apps/agentbox/CLOUDFLARE_ACCESS.md`:

    claude.ai  ->  Cloudflare Access  ->  cloudflared  ->  127.0.0.1:<port>

The server binds LOOPBACK ONLY and refuses any other bind address. That is
not belt-and-braces with the tunnel, it is the thing that makes the tunnel
the only route: a process listening on the public interface would be a
write-capable MCP endpoint with no door in front of it, and no amount of
correct Cloudflare configuration could take that back.

Every request is verified by :mod:`apps.fleet_mcp.access` before it reaches
a tool. The health path is exempt so the unit file has something to probe
that does not need an Access token; it returns liveness only and reads no
fleet state.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from apps.fleet_mcp.access import AccessConfig, AccessRefusal, AccessVerifier
from apps.fleet_mcp.server import create_server

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

#: Answerable without an Access token: liveness only, no fleet state.
HEALTH_PATH = "/healthz"


class BindRefused(RuntimeError):
    """The server was asked to listen somewhere the tunnel could be bypassed."""


def require_loopback(host: str) -> str:
    """Return ``host`` when it is a loopback address, else refuse.

    ``0.0.0.0`` is the specific mistake this catches: it is what a copied
    container recipe usually says, and it would publish the endpoint on every
    interface of a box with a public IP.
    """
    try:
        address = ipaddress.ip_address(host)
    except ValueError as error:
        raise BindRefused(
            f"refusing to bind {host!r}: pass a loopback IP literal, not a name"
        ) from error
    if not address.is_loopback:
        raise BindRefused(
            f"refusing to bind {host!r}: the dispatch endpoint listens on loopback only, "
            "so Cloudflare Access is the only route to it"
        )
    return host


def access_middleware(app: Any, verifier: AccessVerifier) -> Any:
    """Wrap ``app`` so every non-health request proves an allowed identity."""

    async def middleware(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await _refuse(
                send,
                AccessRefusal("transport_refused", "this endpoint serves HTTP only"),
            )
            return
        request = Request(scope, receive=receive)
        if request.url.path.rstrip("/") == HEALTH_PATH:
            await JSONResponse({"status": "ok"})(scope, receive, send)
            return
        try:
            email = verifier.verify(request.headers)
        except AccessRefusal as refusal:
            await _refuse(send, refusal)
            return
        # Carried for tool-side attribution and the bounded access log; the
        # value is the SIGNED claim, not the unsigned header.
        scope.setdefault("state", {})["access_email"] = email
        await app(scope, receive, send)

    return middleware


async def _refuse(send: Callable[..., Awaitable[None]], refusal: AccessRefusal) -> None:
    response = JSONResponse(
        {"error": refusal.code, "message": str(refusal)},
        status_code=403,
    )
    await response({"type": "http"}, _empty_receive, send)


async def _empty_receive() -> dict[str, Any]:  # pragma: no cover - never awaited
    return {"type": "http.disconnect"}


def build_app(*, config: AccessConfig | None = None, verifier: AccessVerifier | None = None) -> Any:
    """The ASGI app: the MCP streamable-HTTP transport behind the Access gate."""
    if verifier is None:
        verifier = AccessVerifier(config or AccessConfig.from_environ())
    return access_middleware(create_server().streamable_http_app(), verifier)


def serve_http(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    """Serve until killed. Refuses a non-loopback bind and an unset Access config."""
    import uvicorn

    require_loopback(host)
    # from_environ raises AccessNotConfigured, so a box with no service
    # environment file never starts a naked endpoint.
    app = build_app(config=AccessConfig.from_environ())
    uvicorn.run(app, host=host, port=port, log_level="info")
