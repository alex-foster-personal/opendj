"""MCP track-edit tools (LIBUX-56): the agent twin of the library's Comment cell editor.

Split out of :mod:`apps.opendj_cli.mcp_server` (600-line ceiling). The tool
calls :func:`apps.opendj_cli.track_comment.set_track_comment`, the same code
``opendj track comment`` runs, so the MCP, CLI and UI paths store the comment
through one route (``PATCH /api/v1/tracks/{id}`` under ``If-Match``).

-Claude
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from apps.opendj_cli.mcp_sdk import MCPServer, ToolAnnotations, ToolError
from apps.opendj_cli.origin import EngineNotRunning, resolve_origin
from apps.opendj_cli.track_comment import TrackCommentError, engine_client, set_track_comment

_MUTATING = ToolAnnotations(read_only_hint=False, destructive_hint=False)


def register_track_tools(
    server: MCPServer,
    *,
    lock_path: Callable[[], Path | None],
    tool_error: Callable[[dict[str, Any]], ToolError],
    engine_not_running: Callable[[EngineNotRunning], ToolError],
) -> None:
    """Register the ``track_*`` edit tools on ``server``."""

    @server.tool(annotations=_MUTATING)
    def track_set_comment(stable_id: str, comment: str) -> dict[str, Any]:
        """Set one library track's comment, as the Comment cell editor does; an empty string clears it.

        Stored as the user's own comment, which the library shows in place of
        the rekordbox comment. Returns the stored value and the track's new ETag.
        """
        try:
            origin = resolve_origin(lock_path())
        except EngineNotRunning as error:
            raise engine_not_running(error) from error
        try:
            with engine_client(origin.base_url) as client:
                return set_track_comment(client, stable_id, comment)
        except TrackCommentError as error:
            raise tool_error(error.to_dict()) from error
        except httpx.TransportError as error:
            raise tool_error(
                {"error": "engine_unreachable", "message": str(error), "origin": origin.base_url}
            ) from error
