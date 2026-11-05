"""MCP tool ``tracks_played_length`` (LIBUX played length).

One call to the running engine's ``POST /api/v1/tracks/played-length``, so the
MCP, HTTP, CLI (``opendj track played-length``) and library footer surfaces
return the same estimate. A non-2xx answer is a tool error carrying the
engine's detail, never an empty result.

-Claude
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from apps.opendj_cli.mcp_sdk import MCPServer, ToolAnnotations, ToolError
from apps.opendj_cli.origin import EngineNotRunning, resolve_origin

PLAYED_LENGTH_PATH = "/api/v1/tracks/played-length"
_TIMEOUT_S = 30.0


def register_played_length_tools(
    server: MCPServer,
    *,
    lock_path: Callable[[], Path | None],
    tool_error: Callable[[dict[str, Any]], ToolError],
    engine_not_running: Callable[[EngineNotRunning], ToolError],
) -> None:
    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    def tracks_played_length(stable_ids: list[str]) -> dict[str, Any]:
        """Rough played length of these tracks when mixed: played_s, total_s and how each was estimated.

        Uses mix points when a track has them, else duration minus a 32-bar
        overlap at its BPM (at least half the track), else 75% of the track.
        """
        try:
            origin = resolve_origin(lock_path())
        except EngineNotRunning as error:
            raise engine_not_running(error) from error
        try:
            response = httpx.post(
                f"{origin.base_url}{PLAYED_LENGTH_PATH}", json={"stable_ids": stable_ids}, timeout=_TIMEOUT_S
            )
        except httpx.TransportError as error:
            raise tool_error({"error": "engine_unreachable", "message": str(error)}) from error
        if not response.is_success:
            raise tool_error({"error": "engine_error", "status_code": response.status_code, "body": response.text[:500]})
        body: dict[str, Any] = response.json()
        return body


__all__ = ["register_played_length_tools"]
