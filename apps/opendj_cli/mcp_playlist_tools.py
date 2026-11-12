"""MCP playlist order tool (LIBUX-8678): the twin of "Save this order".

``playlist_save_order`` makes an order the playlist's stored order in one undo
step, through the same route the playlist header button and ``opendj playlist
save-order`` call. Give ``by`` (a field sort, untagged tracks last) or
``item_ids`` (every live item exactly once). A non-2xx answer is a tool error.

-Claude
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import httpx

from apps.opendj_cli.mcp_sdk import MCPServer, ToolAnnotations, ToolError
from apps.opendj_cli.origin import EngineNotRunning, resolve_origin
from apps.opendj_cli.playlist_cli import SaveOrderFailed, save_order, save_order_body

_REORDERS = ToolAnnotations(read_only_hint=False, destructive_hint=False)
TIMEOUT_S = 30.0

SortField = Literal["bpm", "key", "title", "artist", "duration", "rating", "genre"]


def register_playlist_tools(
    server: MCPServer,
    *,
    lock_path: Callable[[], Path | None],
    tool_error: Callable[[dict[str, Any]], ToolError],
    engine_not_running: Callable[[EngineNotRunning], ToolError],
) -> None:
    """Register ``playlist_save_order`` on ``server``."""

    @server.tool(annotations=_REORDERS)
    def playlist_save_order(
        playlist_id: str,
        by: SortField | None = None,
        descending: bool = False,
        item_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """ "Save this order": make a sort (``by``) or an explicit ``item_ids`` order the playlist's numbering. Undoable."""
        try:
            body = save_order_body(by=by, descending=descending, item_ids=item_ids)
        except ValueError as error:
            raise tool_error({"error": "bad_request", "message": str(error)}) from error
        try:
            origin = resolve_origin(lock_path())
        except EngineNotRunning as error:
            raise engine_not_running(error) from error
        try:
            with httpx.Client(timeout=TIMEOUT_S) as client:
                return save_order(client, origin.base_url, playlist_id, body)
        except SaveOrderFailed as error:
            raise tool_error(
                {"error": "save_order_refused", "status_code": error.status_code, "detail": error.body}
            ) from error
        except httpx.TransportError as error:
            raise tool_error({"error": "engine_unreachable", "message": str(error)}) from error


__all__ = ["register_playlist_tools"]
