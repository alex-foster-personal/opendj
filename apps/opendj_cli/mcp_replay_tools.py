"""MCP replay take-over (SETREC-12067): ``replay_takeover``."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from apps.opendj_cli.mcp_sdk import MCPServer, ToolAnnotations, ToolError
from apps.opendj_cli.replay_cli import takeover_order

_MUTATING = ToolAnnotations(read_only_hint=False, destructive_hint=False)


def register_replay_tools(
    server: MCPServer,
    *,
    run_order: Callable[[dict[str, Any]], dict[str, Any]],
    tool_error: Callable[[dict[str, Any]], ToolError],
) -> None:
    @server.tool(annotations=_MUTATING)
    def replay_takeover(control: str) -> dict[str, Any]:
        """Take over a replaying set (SETREC-12067): stop replay dispatch for one control id
        (dotted, e.g. deck.1.eq.low, mixer.crossfader) or for ``all``; the app is live from there."""
        try:
            order = takeover_order(control)
        except ValueError as error:
            raise tool_error({"error": "usage", "message": str(error)}) from error
        return run_order(order)
