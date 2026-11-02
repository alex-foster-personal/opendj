"""One pure-ASGI middleware for the engine's per-request HTTP guards.

These five layers used to be separate ``@app.middleware("http")`` functions,
each a ``BaseHTTPMiddleware`` that re-streams every response body through an
anyio memory stream and task. A 63 MB deck-audio GET paid that five times per
chunk. This middleware runs the same checks, in the same order, against the
request scope, and touches the response only by adding a header to
``http.response.start``, so body chunks pass straight through.

Order, outermost first, exactly as the decorator stack registered them
(Starlette inserts each new middleware at the outside):

1. bind warning (response header, applied to every response below it)
2. passive usage record
3. share gate (stamps ``request.state.share_audience`` / ``share_read_only``)
4. origin guard
5. host allowlist

Websocket and lifespan scopes bypass it, as ``BaseHTTPMiddleware`` did.
"""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .request_guard import host_allowlist_rejection, origin_guard_rejection
from .share_gate import share_gate_rejection

_LOOPBACK_BIND_HOSTS = frozenset({"127.0.0.1", "localhost"})


def _with_header(send: Send, name: str, value: str) -> Send:
    async def send_with_header(message: Message) -> None:
        if message["type"] == "http.response.start":
            MutableHeaders(scope=message)[name] = value
        await send(message)

    return send_with_header


class HttpGuardMiddleware:
    def __init__(self, app: ASGIApp, *, bind_host: str) -> None:
        self.app = app
        self._bind_warning: str | None = None
        if bind_host and bind_host not in _LOOPBACK_BIND_HOSTS:
            self._bind_warning = f"server is bound to {bind_host}; do not expose without Tailscale"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        warning = self._bind_warning
        if warning is not None:
            send = _with_header(send, "X-Bind-Warning", warning)

        request = Request(scope, receive)
        # Backstop for the heartbeat: ordinary traffic from a real webview or
        # browser is evidence someone has the app open. The store drops
        # telemetry/health paths and non-app user agents, so agents polling
        # this API cannot manufacture the activity they are asking about.
        request.app.state.usage_store.record_request(
            path=request.url.path,
            user_agent=request.headers.get("user-agent", ""),
        )
        for gate in (share_gate_rejection, origin_guard_rejection, host_allowlist_rejection):
            rejection = gate(request)
            if rejection is not None:
                await rejection(scope, receive, send)
                return
        await self.app(scope, receive, send)


__all__ = ["HttpGuardMiddleware"]
