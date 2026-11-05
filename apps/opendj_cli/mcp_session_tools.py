"""MCP session tools (SESSION-PLAYED-01): read, resume or reset the session played set.

Same documents as ``opendj --json session played|resume|new``.

-Claude
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from apps.opendj_cli.mcp_sdk import MCPServer, ToolAnnotations, ToolError
from apps.opendj_cli.origin import EngineNotRunning, resolve_origin
from apps.opendj_cli.session_cli import SessionRequestFailed, request_session

_READ_ONLY = ToolAnnotations(read_only_hint=True)
_MUTATING = ToolAnnotations(read_only_hint=False, destructive_hint=False)
# Clears the live set; the old session is archived on disk, not deleted.
_RESETS = ToolAnnotations(read_only_hint=False, destructive_hint=True)


def register_session_tools(
    server: MCPServer,
    *,
    lock_path: Callable[[], Path | None],
    tool_error: Callable[[dict[str, Any]], ToolError],
    engine_not_running: Callable[[EngineNotRunning], ToolError],
) -> None:
    def _call(verb: str) -> dict[str, Any]:
        try:
            origin = resolve_origin(lock_path())
        except EngineNotRunning as error:
            raise engine_not_running(error) from error
        try:
            return request_session(origin.base_url, verb)
        except SessionRequestFailed as error:
            raise tool_error(
                {"error": "request_failed", "status_code": error.status_code, "detail": error.detail}
            ) from error
        except httpx.TransportError as error:
            raise tool_error(
                {"error": "engine_unreachable", "message": str(error), "origin": origin.base_url}
            ) from error

    @server.tool(annotations=_READ_ONLY)
    def session_played() -> dict[str, Any]:
        """Tracks this DJ session already played (the green library rows). Returns
        ``{session_id, started_at_ms, last_activity_ms, idle_ms, expired, expiry_ms, count,
        played_ids, played: [{stable_id, at_ms}]}``. ``expired`` is true after 12 h idle;
        the ids are kept until ``session_resume`` or ``session_new``."""
        return _call("played")

    @server.tool(annotations=_MUTATING)
    def session_resume() -> dict[str, Any]:
        """Continue the stored session (clears ``expired``), keeping every played id."""
        return _call("resume")

    @server.tool(annotations=_RESETS)
    def session_new() -> dict[str, Any]:
        """Start a new session with an empty played set. The old session is archived on disk."""
        return _call("new")
