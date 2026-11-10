"""MCP crowd-request tools (CROWDREQ-10749).

Same documents as ``opendj --json requests open|close|list|dismiss|loaded|clear``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from apps.opendj_cli.mcp_sdk import MCPServer, ToolAnnotations, ToolError
from apps.opendj_cli.origin import EngineNotRunning, resolve_origin
from apps.opendj_cli.requests_cli import (
    RequestsFailed,
    clear_requests,
    dismiss_request,
    get_session,
    list_requests,
    loaded_request,
    put_session,
)

_READ_ONLY = ToolAnnotations(read_only_hint=True)
_MUTATING = ToolAnnotations(read_only_hint=False, destructive_hint=False)
_CLEARS = ToolAnnotations(read_only_hint=False, destructive_hint=True)

LockPath = Callable[[], Path | None]
ToolErrorFn = Callable[[dict[str, Any]], ToolError]
EngineDownFn = Callable[[EngineNotRunning], ToolError]
Action = Callable[[], dict[str, Any]]


def _base_url(lock_path: LockPath, engine_not_running: EngineDownFn) -> str:
    try:
        return resolve_origin(lock_path()).base_url
    except EngineNotRunning as error:
        raise engine_not_running(error) from error


def _invoke(action: Action, tool_error: ToolErrorFn) -> dict[str, Any]:
    try:
        return action()
    except RequestsFailed as error:
        raise tool_error(
            {"error": "request_failed", "status_code": error.status_code, "detail": error.detail}
        ) from error
    except httpx.TransportError as error:
        raise tool_error({"error": "engine_unreachable", "message": str(error)}) from error


def _register_session_tools(
    server: MCPServer,
    *,
    lock_path: LockPath,
    tool_error: ToolErrorFn,
    engine_not_running: EngineDownFn,
) -> None:
    @server.tool(annotations=_READ_ONLY)
    def requests_session() -> dict[str, Any]:
        """The crowd request session: open, catalog playlist, free text and cooldown."""
        base_url = _base_url(lock_path, engine_not_running)
        return _invoke(lambda: get_session(base_url), tool_error)

    @server.tool(annotations=_MUTATING)
    def requests_open(playlist: str, text: bool = False, cooldown: int = 180) -> dict[str, Any]:
        """Open the crowd request session on a catalog playlist."""
        base_url = _base_url(lock_path, engine_not_running)
        return _invoke(
            lambda: put_session(
                base_url,
                {
                    "open": True,
                    "catalog_playlist_id": playlist,
                    "free_text_allowed": text,
                    "cooldown_s": cooldown,
                },
            ),
            tool_error,
        )

    @server.tool(annotations=_MUTATING)
    def requests_close() -> dict[str, Any]:
        """Close the crowd request session. Existing catalog settings stay."""
        base_url = _base_url(lock_path, engine_not_running)
        return _invoke(lambda: put_session(base_url, {"open": False}), tool_error)


def _register_list_tools(
    server: MCPServer,
    *,
    lock_path: LockPath,
    tool_error: ToolErrorFn,
    engine_not_running: EngineDownFn,
) -> None:
    @server.tool(annotations=_READ_ONLY)
    def requests_list(status: str = "new") -> dict[str, Any]:
        """List crowd requests (status new, loaded, dismissed or all). Newest first."""
        base_url = _base_url(lock_path, engine_not_running)
        return _invoke(lambda: list_requests(base_url, status), tool_error)

    @server.tool(annotations=_MUTATING)
    def requests_dismiss(request_id: str) -> dict[str, Any]:
        """Mark a crowd request dismissed."""
        base_url = _base_url(lock_path, engine_not_running)
        return _invoke(lambda: dismiss_request(base_url, request_id), tool_error)

    @server.tool(annotations=_MUTATING)
    def requests_loaded(request_id: str) -> dict[str, Any]:
        """Mark a crowd request loaded onto a deck."""
        base_url = _base_url(lock_path, engine_not_running)
        return _invoke(lambda: loaded_request(base_url, request_id), tool_error)

    @server.tool(annotations=_CLEARS)
    def requests_clear() -> dict[str, Any]:
        """Delete every crowd request and return how many were removed."""
        base_url = _base_url(lock_path, engine_not_running)
        return _invoke(lambda: clear_requests(base_url), tool_error)


def register_requests_tools(
    server: MCPServer,
    *,
    lock_path: LockPath,
    tool_error: ToolErrorFn,
    engine_not_running: EngineDownFn,
) -> None:
    _register_session_tools(
        server,
        lock_path=lock_path,
        tool_error=tool_error,
        engine_not_running=engine_not_running,
    )
    _register_list_tools(
        server,
        lock_path=lock_path,
        tool_error=tool_error,
        engine_not_running=engine_not_running,
    )
