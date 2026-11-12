"""MCP tools for set recording (SETREC-11802): start, stop and status over HTTP."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from apps.opendj_cli.mcp_sdk import MCPServer, ToolAnnotations, ToolError
from apps.opendj_cli.origin import EngineNotRunning, resolve_origin
from apps.sets.rec_cli import RecCommandFailed, start_body, wait_until_recording
from apps.sets.recorder_service import RecordSource

_TIMEOUT_S = 30.0
_MUTATING = ToolAnnotations(read_only_hint=False, destructive_hint=False)
_READ_ONLY = ToolAnnotations(read_only_hint=True)


def register_rec_tools(
    server: MCPServer,
    *,
    lock_path: Callable[[], Path | None],
    tool_error: Callable[[dict[str, Any]], ToolError],
    engine_not_running: Callable[[EngineNotRunning], ToolError],
) -> None:
    def _request(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        try:
            origin = resolve_origin(lock_path())
        except EngineNotRunning as error:
            raise engine_not_running(error) from error
        try:
            with httpx.Client(timeout=_TIMEOUT_S) as client:
                response = client.request(method, f"{origin.base_url.rstrip('/')}{path}", json=body)
        except httpx.TransportError as error:
            raise tool_error({"error": "engine_unreachable", "message": str(error)}) from error
        try:
            payload = response.json()
        except ValueError as error:
            raise tool_error(
                {"error": "invalid_response", "status_code": response.status_code, "message": response.text}
            ) from error
        if not response.is_success:
            detail = payload.get("detail") if isinstance(payload, dict) else payload
            raise tool_error({"error": "rec_refused", "status_code": response.status_code, "detail": detail})
        return payload

    @server.tool(annotations=_MUTATING)
    def rec_start(
        source: RecordSource,
        device_name: str | None = None,
        sources: list[str] | None = None,
    ) -> dict[str, Any]:
        """Start REC on the running app (master mix, loopback, external input, or tracklist only)."""
        try:
            body = start_body(source, device_name, sources or ["djay_monitor", "opendj_decks"])
        except RecCommandFailed as error:
            raise tool_error({"error": "invalid_request", "message": str(error)}) from error
        base = resolve_origin(lock_path()).base_url
        status = _request("POST", "/api/sets/recorder/start", body)
        try:
            return wait_until_recording(base, status, read=lambda: _request("GET", "/api/sets/recorder"))
        except RecCommandFailed as error:
            raise tool_error({"error": "rec_failed", "message": str(error)}) from error

    @server.tool(annotations=_READ_ONLY)
    def rec_status() -> dict[str, Any]:
        """Return the live recorder status."""
        return _request("GET", "/api/sets/recorder")

    @server.tool(annotations=_MUTATING)
    def rec_stop() -> dict[str, Any]:
        """Stop the active HTTP-owned recording."""
        current = _request("GET", "/api/sets/recorder")
        if not current.get("active"):
            raise tool_error({"error": "not_recording", "message": "nothing is recording"})
        session_id = current.get("session_id")
        if not isinstance(session_id, str):
            raise tool_error({"error": "invalid_status", "message": "active recording has no session_id"})
        return _request("POST", f"/api/sets/recorder/{session_id}/stop")
