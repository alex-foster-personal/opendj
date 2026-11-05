"""MCP set tools (LIBUX-8679): target length, skip-in-set and the set summary.

Each tool is one call to the running engine's ``/api/v1/playlists/{id}/set-*``
routes, so MCP, HTTP and ``opendj set`` return the same document. A non-2xx
answer is a tool error carrying the engine's code, never an empty result.

-Claude
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from apps.opendj_cli.mcp_safety import engine_error_code
from apps.opendj_cli.mcp_sdk import MCPServer, ToolAnnotations, ToolError
from apps.opendj_cli.origin import EngineNotRunning, resolve_origin

PLAYLISTS_PATH = "/api/v1/playlists"
TIMEOUT_S = 30.0
_READ_ONLY = ToolAnnotations(read_only_hint=True)
_MUTATING = ToolAnnotations(read_only_hint=False, destructive_hint=False)


def register_set_tools(
    server: MCPServer,
    *,
    lock_path: Callable[[], Path | None],
    tool_error: Callable[[dict[str, Any]], ToolError],
    engine_not_running: Callable[[EngineNotRunning], ToolError],
) -> None:
    """Register ``set_summary``, ``set_target`` and ``set_skip`` on ``server``."""

    def call(method: str, path: str, json_body: Any = None) -> Any:
        try:
            origin = resolve_origin(lock_path())
        except EngineNotRunning as error:
            raise engine_not_running(error) from error
        try:
            with httpx.Client(timeout=TIMEOUT_S) as client:
                response = client.request(method, f"{origin.base_url}{PLAYLISTS_PATH}{path}", json=json_body)
        except httpx.TransportError as error:
            raise tool_error({"error": "engine_unreachable", "message": str(error)}) from error
        body = response.json()
        if not response.is_success:
            detail = body.get("detail") if isinstance(body, dict) else None
            raise tool_error({"error": engine_error_code(detail, response.status_code),
                              "status_code": response.status_code, "detail": detail or body})
        return body

    @server.tool(annotations=_READ_ONLY)
    def set_summary(playlist_id: str) -> dict[str, Any]:
        """A playlist-as-set's estimated played length, target, over/under and key clashes.

        ``played_s`` sums the non-skipped tracks' estimated played length
        (mix-in to mix-out, else duration minus a 32-bar overlap, at least half
        the duration; no BPM: 75 %). ``over_s`` is negative while under target.
        ``clashes`` lists adjacent non-skipped pairs whose Camelot keys clash
        (skips bridged); ``gross`` marks 3+ wheel steps or a letter change with 2+.
        """
        return call("GET", f"/{playlist_id}/set-summary")

    @server.tool(annotations=_MUTATING)
    def set_target(playlist_id: str, target_s: float | None) -> dict[str, Any]:
        """Set the set's target length in seconds (e.g. 3600); ``null`` clears it. Returns the summary."""
        return call("PUT", f"/{playlist_id}/set-target", {"target_s": target_s})

    @server.tool(annotations=_MUTATING)
    def set_skip(playlist_id: str, stable_id: str, skipped: bool = True) -> dict[str, Any]:
        """Mark a track "skip in set" (kept as a backup, dimmed, left out of the total). Returns the summary."""
        return call("PUT", f"/{playlist_id}/set-skips/{stable_id}", {"skipped": skipped})
