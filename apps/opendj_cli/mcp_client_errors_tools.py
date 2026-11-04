"""MCP ``client_errors`` tool (AGENT-8304): filtered browser errors from any session.

Split out of :mod:`apps.opendj_cli.mcp_server` (600-line ceiling). The tool
calls the same :func:`fetch_client_errors` as ``opendj client_errors``, so the
two surfaces return the same document.

-Claude
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from apps.opendj_cli.client_errors_query import (
    DEFAULT_LIMIT,
    ClientErrorFilters,
    ClientErrorsRequestFailed,
    fetch_client_errors,
)
from apps.opendj_cli.mcp_sdk import MCPServer, ToolAnnotations, ToolError
from apps.opendj_cli.origin import EngineNotRunning, resolve_origin

_READ_ONLY = ToolAnnotations(read_only_hint=True)


def register_client_errors_tools(
    server: MCPServer,
    *,
    lock_path: Callable[[], Path | None],
    tool_error: Callable[[dict[str, Any]], ToolError],
    engine_not_running: Callable[[EngineNotRunning], ToolError],
) -> None:
    """Register ``client_errors`` on ``server``."""

    @server.tool(annotations=_READ_ONLY)
    def client_errors(
        since: str | None = None,
        build_sha: str | None = None,
        kind: str | None = None,
        contains: str | None = None,
        limit: int = DEFAULT_LIMIT,
        untriaged: bool = False,
    ) -> dict[str, Any]:
        """Browser errors the running app recorded (AGENT-8304), newest ``limit``, oldest first.

        Filters, all optional and combined with AND: ``since`` an ISO 8601 time
        with a timezone (e.g. ``2026-11-03T09:00:00Z``); ``build_sha`` a build
        sha prefix (``opendj status`` names the running build); ``kind`` one of
        window-error, unhandled-rejection, sveltekit, ui-error, console-error,
        console-warn, resource-error, csp-violation, webview-console,
        webview-navigation; ``contains`` a case-insensitive message substring;
        ``limit`` 1 to 1000; ``untriaged`` hides errors already triaged.
        Returns ``{count, filters, errors: [{timestamp, build_sha, kind,
        message, event_id}]}``, the same document as ``opendj --json
        client_errors``. A bad filter is a tool error carrying the engine's 422
        detail, never an empty result.
        """
        try:
            origin = resolve_origin(lock_path())
        except EngineNotRunning as error:
            raise engine_not_running(error) from error
        filters = ClientErrorFilters(
            since=since, build_sha=build_sha, kind=kind, contains=contains, limit=limit, untriaged=untriaged
        )
        try:
            return fetch_client_errors(origin.base_url, filters)
        except ClientErrorsRequestFailed as error:
            raise tool_error(
                {"error": "request_failed", "status_code": error.status_code, "detail": error.detail}
            ) from error
        except httpx.TransportError as error:
            raise tool_error(
                {"error": "engine_unreachable", "message": str(error), "origin": origin.base_url}
            ) from error
