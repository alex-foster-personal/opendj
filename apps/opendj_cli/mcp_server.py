"""Stdio MCP server for installed Open DJ (AGENT-11).

Six tools over lock-file HTTP: ``status``, ``app_state``, ``command``,
``library``, ``ui_url``, and ``open_route``. Safety rails live in
:mod:`mcp_safety`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from apps.opendj_cli.api_cli import HTTP_METHODS, _parse_field, _parse_header, _request_body
from apps.opendj_cli.client import (
    MIRROR_TIMEOUT_S,
    EngineClient,
    MalformedResult,
    NoPerformancePage,
    OrderRejected,
    OrderTimedOut,
)
from apps.opendj_cli.mcp_safety import (
    SafetyRefusal,
    guard_library_request,
    guard_order,
    validate_api_path,
)
from apps.opendj_cli.orders import single
from apps.opendj_cli.origin import EngineNotRunning, EngineOrigin, resolve_origin, unreachable
from apps.opendj_cli.shell_navigate import REMEDY_VERB, open_shell_route
from apps.opendj_cli.verbs import InvocationError, parse_invocation

SERVER_NAME = "opendj-mcp"
SERVER_VERSION = "0.1.0"
PROBE_TIMEOUT_S = 3.0
UI_URL_NOTE = (
    "Open in browser MCP only; does not move the installed desktop shell window. "
    "Use open_route for shell navigation (AGENT-12). Release builds have no "
    "WebDriver seam (AGENT-07)."
)

_READ_ONLY = ToolAnnotations(read_only_hint=True)
_MUTATING = ToolAnnotations(read_only_hint=False)
_DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True)


@dataclass
class _SessionState:
    lock_path: Path | None = None
    last_mirror_delta: dict[str, Any] | None = None
    safety_notes: dict[str, Any] = field(default_factory=dict)


_state = _SessionState()


def _engine_not_running(error: EngineNotRunning) -> dict[str, Any]:
    return {
        "error": "engine_not_running",
        "lock_path": str(error.lock_path),
        "message": str(error),
    }


def _resolve_origin() -> EngineOrigin:
    return resolve_origin(_state.lock_path)


def _fetch_json(origin: EngineOrigin, path: str) -> Any:
    url = f"{origin.base_url}{path}"
    try:
        with httpx.Client(timeout=PROBE_TIMEOUT_S) as client:
            response = client.get(url)
    except httpx.TransportError as error:
        raise unreachable(origin, error) from error
    if response.status_code != 200:
        return f"UNREACHABLE: {response.status_code} {response.text[:200]}"
    try:
        return response.json()
    except ValueError:
        return response.text


def _parse_order_payload(
    order: dict[str, Any] | None,
    verb: str | None,
    args: list[str] | None,
) -> dict[str, Any]:
    if order is not None:
        return order
    if verb is None:
        raise SafetyRefusal(
            "usage",
            "command requires either order or verb",
        )
    tokens = [verb, *(args or [])]
    try:
        invocation = parse_invocation(tokens)
    except InvocationError as error:
        raise SafetyRefusal("usage", str(error)) from error
    return single(invocation)


def _parse_library_fields(raw: dict[str, str] | None) -> tuple[tuple[str, Any], ...]:
    if not raw:
        return ()
    return tuple(_parse_field(f"{key}={value}") for key, value in raw.items())


def _parse_library_headers(raw: dict[str, str] | None) -> tuple[tuple[str, str], ...]:
    if not raw:
        return ()
    return tuple(_parse_header(f"{key}:{value}") for key, value in raw.items())


def _parse_app_state_body(response: httpx.Response) -> Any:
    content_type = response.headers.get("content-type", "")
    if "json" in content_type.lower():
        try:
            return response.json()
        except ValueError:
            pass
    return response.text


def create_server() -> MCPServer:
    server = MCPServer(name=SERVER_NAME, version=SERVER_VERSION)

    @server.tool(annotations=_READ_ONLY)
    def status() -> dict[str, Any]:
        """Engine origin from the lock file, health, and build-info."""
        try:
            origin = _resolve_origin()
        except EngineNotRunning as error:
            return _engine_not_running(error)
        try:
            health = _fetch_json(origin, "/api/v1/health")
            build_info = _fetch_json(origin, "/api/v1/build-info")
        except EngineNotRunning as error:
            return _engine_not_running(error)
        return {
            "lock_path": str(origin.lock_path),
            "origin": origin.base_url,
            "pid": origin.pid,
            "role": origin.role,
            "health": health,
            "build_info": build_info,
        }

    @server.tool(annotations=_READ_ONLY, structured_output=True)
    def app_state(path: str = "/api/v1/setup/status") -> dict[str, Any]:
        """GET-only proxy to ``/api/v1/*`` on the running engine."""
        try:
            validate_api_path(path)
            origin = _resolve_origin()
        except SafetyRefusal as error:
            return {"error": error.code, **error.fields, "message": str(error)}
        except EngineNotRunning as error:
            return _engine_not_running(error)
        url = f"{origin.base_url}{path}"
        try:
            with httpx.Client(timeout=PROBE_TIMEOUT_S) as client:
                response = client.get(url)
        except httpx.TransportError as error:
            return _engine_not_running(unreachable(origin, error))
        if not response.is_success:
            raise ToolError(
                f"engine GET {path} -> {response.status_code}: {response.text[:500]}"
            )
        body = _parse_app_state_body(response)
        if isinstance(body, dict):
            return body
        return {"body": body}

    @server.tool(annotations=_DESTRUCTIVE)
    def command(
        order: dict[str, Any] | None = None,
        verb: str | None = None,
        args: list[str] | None = None,
    ) -> dict[str, Any]:
        """Dispatch one AGENT-03 order and return the page result document."""
        try:
            origin = _resolve_origin()
            parsed_order = _parse_order_payload(order, verb, args)
            guarded_order, safety = guard_order(
                parsed_order,
                last_mirror_delta=_state.last_mirror_delta,
            )
        except SafetyRefusal as error:
            return {"error": error.code, "message": str(error), **error.fields}
        except EngineNotRunning as error:
            return _engine_not_running(error)

        client = EngineClient(origin=origin)
        try:
            result = client.post_order(guarded_order)
        except NoPerformancePage as error:
            return {
                "error": "no_performance_page",
                "origin": origin.base_url,
                "remedy": REMEDY_VERB,
                "message": str(error),
            }
        except (OrderRejected, OrderTimedOut, MalformedResult) as error:
            return {"error": "order_failed", "message": str(error)}
        except EngineNotRunning as error:
            return _engine_not_running(error)

        mirror_delta = result.get("mirror_delta")
        if isinstance(mirror_delta, dict):
            _state.last_mirror_delta = mirror_delta
        document: dict[str, Any] = dict(result)
        if safety:
            document["safety"] = safety
        return document

    @server.tool(annotations=_DESTRUCTIVE)
    def library(
        method: str,
        path: str,
        body: str | None = None,
        fields: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Raw library HTTP against the engine origin (LIBM-11).

        ``body`` carries the same JSON string ``opendj api --json`` accepts.
        The name ``json`` is reserved by the MCP wire format, so this tool
        exposes ``body`` instead.
        """
        upper = method.upper()
        if upper not in HTTP_METHODS:
            return {"error": "usage", "message": f"unknown HTTP method {method!r}"}
        if not path.startswith("/"):
            return {"error": "usage", "message": f"PATH must start with '/', got {path!r}"}
        try:
            guard_library_request(upper, path)
            origin = _resolve_origin()
            request_body = _request_body(
                upper,
                body,
                _parse_library_fields(fields),
            )
        except SafetyRefusal as error:
            return {"error": error.code, **error.fields, "message": str(error)}
        except EngineNotRunning as error:
            return _engine_not_running(error)
        except ValueError as error:
            return {"error": "usage", "message": str(error)}

        url = f"{origin.base_url}{path}"
        request_headers = {"Accept": "application/json"}
        for name, value in _parse_library_headers(headers):
            request_headers[name] = value
        if request_body is not None:
            request_headers["Content-Type"] = "application/json"
        try:
            with httpx.Client(timeout=MIRROR_TIMEOUT_S) as client:
                response = client.request(
                    upper,
                    url,
                    headers=request_headers,
                    content=request_body,
                )
        except httpx.TransportError as error:
            return _engine_not_running(unreachable(origin, error))

        payload: dict[str, Any] = {
            "status_code": response.status_code,
            "body": response.text,
        }
        if response.headers:
            payload["headers"] = dict(response.headers)
        return payload

    @server.tool(annotations=_MUTATING)
    def open_route(route: str = "/performance") -> dict[str, Any]:
        """Navigate the installed desktop shell to a route (AGENT-12)."""
        if not route.startswith("/"):
            route = f"/{route}"
        try:
            origin = _resolve_origin()
        except EngineNotRunning as error:
            return _engine_not_running(error)
        try:
            if route != "/performance" and not route.startswith("/performance/"):
                return {
                    "error": "usage",
                    "message": f"route {route!r} is not allowlisted; use /performance",
                }
            result = open_shell_route(origin, route)
        except NoPerformancePage as error:
            return {
                "error": "no_performance_page",
                "remedy": REMEDY_VERB,
                "origin": origin.base_url,
                "message": str(error),
            }
        except EngineNotRunning as error:
            return _engine_not_running(error)
        return {"accepted": True, **result}

    @server.tool(annotations=_READ_ONLY)
    def ui_url(route: str = "/") -> dict[str, Any]:
        """Return the engine-served SPA URL for browser MCP driving."""
        if not route.startswith("/"):
            route = f"/{route}"
        try:
            origin = _resolve_origin()
        except EngineNotRunning as error:
            return _engine_not_running(error)
        url = f"{origin.base_url}{route}"
        document: dict[str, Any] = {"url": url, "note": UI_URL_NOTE}
        try:
            with httpx.Client(timeout=PROBE_TIMEOUT_S) as client:
                response = client.get(url)
            if response.status_code == 404:
                document["reachable"] = False
            else:
                document["reachable"] = response.is_success
        except httpx.TransportError:
            document["reachable"] = False
        return document

    return server


def run_stdio(lock_path: Path | None = None) -> None:
    """Start the stdio MCP server until stdin closes."""
    _state.lock_path = lock_path
    create_server().run(transport="stdio")
