"""SETREC-12874: shipped MCP adapters for the daemon's REC lifecycle routes."""

from __future__ import annotations

import json
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import quote

import httpx
from pydantic import Field

from apps.opendj_cli.mcp_safety import (
    SafetyRefusal,
    engine_error_code,
    guard_library_request,
    validate_api_path,
)
from apps.opendj_cli.mcp_sdk import MCPServer, ToolAnnotations, ToolError
from apps.opendj_cli.origin import EngineNotRunning, resolve_origin

RECORDER_PATH = "/api/sets/recorder"
TIMEOUT_S = 30.0
SessionId = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}(?:_\d+)?$")]
Source = Literal["master", "loopback", "external", "none"]
EventSource = Literal["djay_monitor", "rb_history", "opendj_decks"]
_READ_ONLY = ToolAnnotations(read_only_hint=True)
_MUTATING = ToolAnnotations(read_only_hint=False, destructive_hint=False)


def _recorder_request(
    method: str,
    suffix: str = "",
    body: dict[str, Any] | None = None,
    *,
    lock_path: Callable[[], Path | None],
    tool_error: Callable[[dict[str, Any]], ToolError],
    engine_not_running: Callable[[EngineNotRunning], ToolError],
) -> dict[str, Any]:
    """Forward one guarded request, retaining daemon failures without flattening detail."""
    path = f"{RECORDER_PATH}{suffix}"
    try:
        validate_api_path(path, method=method)
        guard_library_request(method, path)
    except SafetyRefusal as error:
        raise tool_error({"error": error.code, "message": str(error), **error.fields}) from error
    try:
        origin = resolve_origin(lock_path())
    except EngineNotRunning as error:
        raise engine_not_running(error) from error
    try:
        with httpx.Client(timeout=TIMEOUT_S) as client:
            response = client.request(method, f"{origin.base_url}{path}", json=body)
    except httpx.TransportError as error:
        raise tool_error({"error": "engine_unreachable", "message": str(error)}) from error
    try:
        document = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        if response.is_success:
            raise tool_error(
                {
                    "error": "invalid_response",
                    "status_code": response.status_code,
                    "message": "recorder response is not JSON",
                }
            ) from error
        document = response.text
    if not response.is_success:
        detail = document["detail"] if isinstance(document, dict) and "detail" in document else document
        raise tool_error(
            {
                "error": engine_error_code(detail, response.status_code),
                "status_code": response.status_code,
                "detail": detail,
            }
        )
    if not isinstance(document, dict):
        raise tool_error(
            {
                "error": "invalid_response",
                "status_code": response.status_code,
                "message": "recorder response is not an object",
            }
        )
    return document


def register_recorder_tools(
    server: MCPServer,
    *,
    lock_path: Callable[[], Path | None],
    tool_error: Callable[[dict[str, Any]], ToolError],
    engine_not_running: Callable[[EngineNotRunning], ToolError],
) -> None:
    """Register status, start, stop and stale-owner recovery using REC's API."""
    call = partial(_recorder_request, lock_path=lock_path, tool_error=tool_error, engine_not_running=engine_not_running)

    @server.tool(annotations=_READ_ONLY)
    def recorder_status() -> dict[str, Any]:
        """Read REC status, including owner PID, recoverability and capture diagnostics."""
        return call("GET")

    @server.tool(annotations=_MUTATING)
    def recorder_start(
        source: Source,
        session_id: SessionId | None = None,
        device_name: Annotated[str, Field(min_length=1, max_length=256)] | None = None,
        ffmpeg_device_idx: Annotated[int, Field(ge=0)] | None = None,
        sources: list[EventSource] | None = None,
    ) -> dict[str, Any]:
        """Start REC with an explicit capture source; none records only the tracklist.

        External/loopback sources require exactly one input name or device index.
        Omitted sources retain the daemon default; [] disables event pollers.
        Returns current capture state, including transitional states and errors.
        """
        body = {
            key: value
            for key, value in {
                "source": source,
                "session_id": session_id,
                "device_name": device_name,
                "ffmpeg_device_idx": ffmpeg_device_idx,
                "sources": sources,
            }.items()
            if value is not None
        }
        return call("POST", "/start", body)

    @server.tool(annotations=_MUTATING)
    def recorder_stop(session_id: SessionId) -> dict[str, Any]:
        """Stop and finalize the REC session observed in recorder_status."""
        return call("POST", f"/{quote(session_id, safe='')}/stop")

    @server.tool(annotations=_MUTATING)
    def recorder_recover(session_id: SessionId, expected_pid: Annotated[int, Field(gt=0)]) -> dict[str, Any]:
        """Finalize a stale recorder using its session and PID from recorder_status.

        A live or changed owner is refused by the daemon. Never refresh and retry
        automatically: inspect status again before making another recovery call.
        """
        return call("POST", f"/{quote(session_id, safe='')}/recover", {"expected_pid": expected_pid})
