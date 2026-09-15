"""MCP update tools (AGENT-13, issue #2942).

Split out of :mod:`apps.opendj_cli.mcp_server` to keep that module under the
repo's 600-line ceiling. The tools are the agent-native half of the updater:
they call the SAME ``check_via_engine`` and ``apply_via_engine`` the packaged
CLI calls, so the two surfaces cannot drift on what "installed" means.

The three callables are injected rather than imported, so this module does not
import the server module that registers it.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from apps.engine_core.update_channel import (
    APPLY_TIMEOUT_S,
    EXIT_APPLIED,
    apply_via_engine,
    check_via_engine,
)
from apps.opendj_cli.mcp_safety import SafetyRefusal, guard_update_apply
from apps.opendj_cli.origin import EngineNotRunning, resolve_origin

_READ_ONLY = ToolAnnotations(read_only_hint=True)
_DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True)


def register_update_tools(
    server: MCPServer,
    *,
    lock_path: Callable[[], Path | None],
    tool_error: Callable[[dict[str, Any]], ToolError],
    engine_not_running: Callable[[EngineNotRunning], ToolError],
) -> None:
    """Register ``update_check`` and ``update_apply`` on ``server``."""

    @server.tool(annotations=_READ_ONLY)
    def update_check() -> dict[str, Any]:
        """Report what the update channel offers the running app (AGENT-13).

        Read-only: one manifest is read and two versions are compared, and
        nothing is downloaded or installed. A status an agent cannot act on
        is an error result, so an outage can never be read as "up to date".
        """
        try:
            origin = resolve_origin(lock_path())
        except EngineNotRunning as error:
            raise engine_not_running(error) from error
        outcome = check_via_engine(origin.base_url)
        if not outcome.actionable:
            raise tool_error(
                {
                    "error": outcome.status,
                    "detail": outcome.detail,
                    "origin": origin.base_url,
                    "document": outcome.document,
                }
            )
        return outcome.document

    @server.tool(annotations=_DESTRUCTIVE)
    def update_apply(timeout_s: float = APPLY_TIMEOUT_S) -> dict[str, Any]:
        """Install the announced release and wait for the relaunch (AGENT-13).

        Gated like the other destructive calls, because it replaces the
        installed app. Success is claimed only when the relaunched app
        reports the announced version AND a build identity that moved.
        """
        try:
            guard_update_apply()
            origin = resolve_origin(lock_path())
        except SafetyRefusal as error:
            raise tool_error(
                {"error": error.code, "message": str(error), **error.fields}
            ) from error
        except EngineNotRunning as error:
            raise engine_not_running(error) from error
        if not timeout_s > 0:
            raise tool_error(
                {
                    "error": "usage",
                    "message": f"timeout_s must be greater than zero, got {timeout_s!r}",
                }
            )
        outcome = apply_via_engine(
            origin.base_url, timeout_s, lock_path=lock_path()
        )
        if outcome.code != EXIT_APPLIED:
            raise tool_error(
                {
                    "error": outcome.reason,
                    "status": outcome.status,
                    "detail": outcome.detail,
                    "origin": origin.base_url,
                    "command_id": outcome.command_id,
                    "available_version": outcome.advertised_version,
                    "before": outcome.before,
                    "after": outcome.after,
                }
            )
        return {
            "applied": True,
            "available_version": outcome.advertised_version,
            "command_id": outcome.command_id,
            "before": outcome.before,
            "after": outcome.after,
        }
