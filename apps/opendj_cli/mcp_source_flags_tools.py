"""MCP tool ``library_source_flags`` (SRCFLAG-01..03).

One call to the running engine's ``/api/v1/source-flags`` routes, so the MCP,
HTTP, CLI (``opendj track source-flags``) and library Err column report the
same flag-only findings. A non-2xx answer is a tool error carrying the
engine's detail, never an empty result.

-Claude
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import httpx

from apps.opendj_cli.mcp_sdk import MCPServer, ToolAnnotations, ToolError
from apps.opendj_cli.origin import EngineNotRunning, resolve_origin

SOURCE_FLAGS_PATH = "/api/v1/source-flags"
_TIMEOUT_S = 120.0


def register_source_flags_tools(
    server: MCPServer,
    *,
    lock_path: Callable[[], Path | None],
    tool_error: Callable[[dict[str, Any]], ToolError],
    engine_not_running: Callable[[EngineNotRunning], ToolError],
) -> None:
    def _call(method: str, path: str) -> dict[str, Any]:
        try:
            origin = resolve_origin(lock_path())
        except EngineNotRunning as error:
            raise engine_not_running(error) from error
        try:
            response = httpx.request(method, f"{origin.base_url}{path}", timeout=_TIMEOUT_S)
        except httpx.TransportError as error:
            raise tool_error({"error": "engine_unreachable", "message": str(error)}) from error
        if not response.is_success:
            raise tool_error({"error": "engine_error", "status_code": response.status_code, "body": response.text[:500]})
        body: dict[str, Any] = response.json()
        return body

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    def library_source_flags(
        stable_id: str | None = None,
        code: Literal["all", "length_mismatch", "stems_other_recording", "hardlinked_write"] = "all",
    ) -> dict[str, Any]:
        """Tracks whose file may not be the recording it claims, from the last scan (flag-only).

        length_mismatch: file length is over 1 % off its Spotify release.
        stems_other_recording: the stem-set was made from a different-length source.
        hardlinked_write: the file was written in place through a hard link.
        Pass stable_id for one track. Rescan with library_source_flags_scan.
        """
        if stable_id is not None:
            return _call("GET", f"{SOURCE_FLAGS_PATH}/{stable_id}")
        return _call("GET", f"{SOURCE_FLAGS_PATH}?code={code}")

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True))
    def library_source_flags_scan() -> dict[str, Any]:
        """Rescan the library for source flags and store the result. Reads music files' headers only; changes no music."""
        return _call("POST", f"{SOURCE_FLAGS_PATH}/scan")


__all__ = ["register_source_flags_tools"]
