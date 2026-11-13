"""MCP update tools (AGENT-13, issue #2942).

Split out of :mod:`apps.opendj_cli.mcp_server` to keep that module under the
repo's 600-line ceiling. The tools are the agent-native half of the updater:
they call the SAME ``check_via_engine`` and ``apply_via_engine`` the packaged
CLI calls, so the two surfaces cannot drift on what "installed" means.

The three callables are injected rather than imported, so this module does not
import the server module that registers it.

Persona cli-mcp runs (issue #11277) never call :func:`update_apply`; the harness
refuses that tool before stdio dispatch.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from apps.engine_core.local_update_client import (
    cancel_via_engine,
    install_via_engine,
    ready_via_engine,
    status_via_engine,
)
from apps.engine_core.update_channel import (
    APPLY_TIMEOUT_S,
    EXIT_APPLIED,
    apply_via_engine,
    check_via_engine,
)
from apps.opendj_cli.mcp_safety import SafetyRefusal, guard_update_apply
from apps.opendj_cli.mcp_sdk import MCPServer, ToolAnnotations, ToolError
from apps.opendj_cli.origin import EngineNotRunning, resolve_origin

_READ_ONLY = ToolAnnotations(read_only_hint=True)
_DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True)
#: Flagging a build writes one record and installs nothing, so it is neither.
_RECORDS = ToolAnnotations(read_only_hint=False, destructive_hint=False)


def register_update_tools(
    server: MCPServer,
    *,
    lock_path: Callable[[], Path | None],
    tool_error: Callable[[dict[str, Any]], ToolError],
    engine_not_running: Callable[[EngineNotRunning], ToolError],
) -> None:
    """Register ``update_check`` and ``update_apply``, then the INSTALL-39 tools."""
    register_app_update_tools(
        server, lock_path=lock_path, tool_error=tool_error, engine_not_running=engine_not_running
    )

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


def register_app_update_tools(
    server: MCPServer,
    *,
    lock_path: Callable[[], Path | None],
    tool_error: Callable[[dict[str, Any]], ToolError],
    engine_not_running: Callable[[EngineNotRunning], ToolError],
) -> None:
    """Register restart-to-update for a local build (INSTALL-39).

    ``app_update_ready``/``app_update_status``/``app_update_install`` call the
    same client core as ``opendj update ready|status|install``.
    """

    def _origin() -> str:
        try:
            return resolve_origin(lock_path()).base_url
        except EngineNotRunning as error:
            raise engine_not_running(error) from error

    @server.tool(annotations=_RECORDS)
    def app_update_ready(artifact: str, notes: str | None = None) -> dict[str, Any]:
        """Flag a local build as ready to install; the app shows "Restart to update".

        ``artifact`` is the updater archive (``.../bundle/macos/Open DJ.app.tar.gz``)
        with its ``.sig`` beside it. The engine reads the build stamp from the
        archive and records it; nothing is installed until ``app_update_install``
        or the user clicks the nudge.
        """
        reply = ready_via_engine(_origin(), Path(artifact), notes)
        if not reply.ok:
            raise tool_error({"error": reply.error, "detail": reply.detail})
        return reply.document

    @server.tool(annotations=_READ_ONLY)
    def app_update_status() -> dict[str, Any]:
        """What local build is pending, and whether it is the one running (INSTALL-39)."""
        reply = status_via_engine(_origin())
        if not reply.ok:
            raise tool_error({"error": reply.error, "detail": reply.detail})
        return reply.document

    @server.tool(annotations=_DESTRUCTIVE)
    def app_update_install(timeout_s: float = APPLY_TIMEOUT_S) -> dict[str, Any]:
        """Install the pending local build and wait for the relaunch (INSTALL-39).

        The same action as clicking "Restart to update". Gated like
        ``update_apply``. The app first shows a 10 s countdown (INSTALL-43);
        ``app_update_cancel`` or Esc Space x4 in the app withdraws it. The shell's updater verifies the archive's signature
        against its compiled-in key before replacing the bundle; success is
        claimed only when the relaunched app reports the flagged git sha.
        """
        try:
            guard_update_apply()
        except SafetyRefusal as error:
            raise tool_error({"error": error.code, "message": str(error), **error.fields}) from error
        if not timeout_s > 0:
            raise tool_error({"error": "usage", "message": f"timeout_s must be greater than zero, got {timeout_s!r}"})
        outcome = install_via_engine(_origin(), timeout_s, lock_path=lock_path())
        if not outcome.installed:
            raise tool_error(outcome.document())
        return outcome.document()

    register_app_update_cancel(server, origin=_origin, tool_error=tool_error)


def register_app_update_cancel(
    server: MCPServer,
    *,
    origin: Callable[[], str],
    tool_error: Callable[[dict[str, Any]], ToolError],
) -> None:
    """Register ``app_update_cancel``, the MCP twin of Esc Space x4 (INSTALL-43)."""

    @server.tool(annotations=_RECORDS)
    def app_update_cancel() -> dict[str, Any]:
        """Cancel the local install still counting down (INSTALL-43).

        The same action as pressing Esc Space Esc Space Esc Space Esc Space in
        the app. Refused with ``not-counting-down`` when no install is waiting,
        and ``already-installing`` once the shell has started.
        """
        reply = cancel_via_engine(origin(), "MCP app_update_cancel")
        if not reply.ok:
            raise tool_error({"error": reply.error, "detail": reply.detail})
        return reply.document
