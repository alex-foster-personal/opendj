"""MCP tool ``tracks_list_totals`` (LIBUX list footer).

One call to the running engine's ``POST /api/v1/tracks/list-totals``.

-Cursor
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from apps.opendj_cli.mcp_sdk import MCPServer, ToolAnnotations, ToolError
from apps.opendj_cli.origin import EngineNotRunning, resolve_origin

LIST_TOTALS_PATH = "/api/v1/tracks/list-totals"
_TIMEOUT_S = 30.0


def register_list_totals_tools(
    server: MCPServer,
    *,
    lock_path: Callable[[], Path | None],
    tool_error: Callable[[dict[str, Any]], ToolError],
    engine_not_running: Callable[[EngineNotRunning], ToolError],
) -> None:
    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    def tracks_list_totals(stable_ids: list[str]) -> dict[str, Any]:
        """Track count and summed duration for these stable ids: count, duration_ms, unknown."""
        try:
            origin = resolve_origin(lock_path())
        except EngineNotRunning as error:
            raise engine_not_running(error) from error
        try:
            response = httpx.post(
                f"{origin.base_url}{LIST_TOTALS_PATH}", json={"stable_ids": stable_ids}, timeout=_TIMEOUT_S
            )
        except httpx.TransportError as error:
            raise tool_error({"error": "engine_unreachable", "message": str(error)}) from error
        if not response.is_success:
            raise tool_error(
                {"error": "engine_error", "status_code": response.status_code, "body": response.text[:500]}
            )
        body: dict[str, Any] = response.json()
        return body


__all__ = ["register_list_totals_tools"]
