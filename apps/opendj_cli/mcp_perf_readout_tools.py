"""MCP tool ``perf_readouts``: the top-bar performance card rows (PERFREADOUT-12579).

Same read as ``opendj perf_readouts`` (:func:`read_perf_readouts`), so both
return the same serialized rows. Read-only: no tool writes the telemetry sink.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from apps.opendj_cli.mcp_sdk import MCPServer, ToolAnnotations, ToolError
from apps.opendj_cli.origin import EngineNotRunning, resolve_origin
from apps.opendj_cli.perf_readouts_cli import PerfReadoutsUnavailable, read_perf_readouts


def register_perf_readout_tools(
    server: MCPServer,
    *,
    lock_path: Callable[[], Path | None],
    tool_error: Callable[[dict[str, Any]], ToolError],
    engine_not_running: Callable[[EngineNotRunning], ToolError],
) -> None:
    """Register the read-only ``perf_readouts`` tool."""

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    def perf_readouts() -> dict[str, Any]:
        """Top-bar performance card rows (key, name, value, explainer, detail) from the last page sample."""
        try:
            origin = resolve_origin(lock_path())
        except EngineNotRunning as error:
            raise engine_not_running(error) from error
        try:
            return read_perf_readouts(origin.base_url)
        except PerfReadoutsUnavailable as error:
            code = "no_readouts" if error.status_code == 404 else "engine_error"
            raise tool_error({"error": code, "status_code": error.status_code, "detail": error.detail}) from error
        except httpx.HTTPError as error:
            raise tool_error({"error": "engine_unreachable", "message": str(error)}) from error


__all__ = ["register_perf_readout_tools"]
