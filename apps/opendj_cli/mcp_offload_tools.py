"""MCP cloud offload tools (OFFLOAD, spec ``(private path)``).

One tool per context-menu action, each a single call to the running engine's
``/api/v1/offload`` routes, so the MCP, HTTP and CLI (``opendj offload``)
surfaces return the same documents. UI tests drive offload through these
tools, never by writing the ledger.

The loop an agent follows: ``offload_tracks`` or ``offload_playlist`` returns
an operation; poll ``offload_operation`` for bytes and counts until its state
is ``done`` / ``failed`` / ``cancelled``; ``offload_restore`` with mode
``temporary`` ("Download for 24h") or ``permanent`` ("Re-download
permanently") brings tracks back.

``offload_tracks`` deletes local files, so it carries ``destructive_hint``. It
is not behind ``OPENDJ_MCP_ENABLE_DESTRUCTIVE``: nothing is deleted until R2 is
verified to hold the same bytes, so every offload is undone by a restore. A
non-2xx answer is a tool error carrying the engine's ``code``.

-Claude
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx

from apps.opendj_cli.mcp_safety import engine_error_code
from apps.opendj_cli.mcp_sdk import MCPServer, ToolAnnotations, ToolError
from apps.opendj_cli.origin import EngineNotRunning, resolve_origin

_READ_ONLY = ToolAnnotations(read_only_hint=True)
_MUTATING = ToolAnnotations(read_only_hint=False, destructive_hint=False)
_FREES_DISK = ToolAnnotations(read_only_hint=False, destructive_hint=True)
OFFLOAD_PATH = "/api/v1/offload"
TIMEOUT_S = 60.0

RestoreMode = Literal["temporary", "permanent"]


@dataclass(frozen=True)
class _Hooks:
    lock_path: Callable[[], Path | None]
    tool_error: Callable[[dict[str, Any]], ToolError]
    engine_not_running: Callable[[EngineNotRunning], ToolError]


def _call(hooks: _Hooks, method: str, path: str, *, json_body: Any, params: Any) -> Any:
    """One request to the offload routes; a non-2xx answer is a tool error with its code."""
    try:
        origin = resolve_origin(hooks.lock_path())
    except EngineNotRunning as error:
        raise hooks.engine_not_running(error) from error
    try:
        with httpx.Client(timeout=TIMEOUT_S) as client:
            response = client.request(method, f"{origin.base_url}{OFFLOAD_PATH}{path}", json=json_body, params=params)
    except httpx.TransportError as error:
        raise hooks.tool_error({"error": "engine_unreachable", "message": str(error)}) from error
    try:
        body = response.json()
    except ValueError:
        raise hooks.tool_error(
            {"error": "engine_bad_response", "status_code": response.status_code, "text": response.text[:500]}
        ) from None
    if not response.is_success:
        detail = body.get("detail") if isinstance(body, dict) else None
        raise hooks.tool_error(
            {"error": engine_error_code(detail, response.status_code), "status_code": response.status_code,
             "detail": detail or body}
        )
    return body


def register_offload_tools(
    server: MCPServer,
    *,
    lock_path: Callable[[], Path | None],
    tool_error: Callable[[dict[str, Any]], ToolError],
    engine_not_running: Callable[[EngineNotRunning], ToolError],
) -> None:
    """Register the ``offload_*`` tools on ``server``."""
    hooks = _Hooks(lock_path, tool_error, engine_not_running)

    def call(method: str, path: str, *, json_body: Any = None, params: Any = None) -> Any:
        return _call(hooks, method, path, json_body=json_body, params=params)

    @server.tool(annotations=_FREES_DISK)
    def offload_tracks(stable_ids: list[str]) -> dict[str, Any]:
        """"Offload to cloud": upload each track's audio and stems to R2, verify, then delete them here.

        Returns an operation; poll ``offload_operation``. A track on a deck or
        during a gig is refused (``TRACK_ON_DECK`` / ``OFFLOAD_DEFERRED``).
        """
        return call("POST", "/tracks", json_body={"stable_ids": stable_ids})

    @server.tool(annotations=_FREES_DISK)
    def offload_playlist(playlist_id: str) -> dict[str, Any]:
        """"Offload to cloud" for every track in a playlist. Returns an operation."""
        return call("POST", f"/playlists/{playlist_id}")

    @server.tool(annotations=_MUTATING)
    def offload_restore(stable_ids: list[str], mode: RestoreMode) -> dict[str, Any]:
        """Bring offloaded tracks back: ``temporary`` = "Download for 24h", ``permanent`` = "Re-download permanently"."""
        return call("POST", "/restore", json_body={"stable_ids": stable_ids, "mode": mode})

    @server.tool(annotations=_MUTATING)
    def offload_restore_playlist(playlist_id: str, mode: RestoreMode) -> dict[str, Any]:
        """Restore every offloaded track of a playlist (``temporary`` or ``permanent``)."""
        return call("POST", f"/playlists/{playlist_id}/restore", json_body={"mode": mode})

    @server.tool(annotations=_READ_ONLY)
    def offload_operation(op_id: str) -> dict[str, Any]:
        """One operation's progress: state, bytes_done/bytes_total, tracks_done/tracks_total, per-track results."""
        return call("GET", f"/operations/{op_id}")

    @server.tool(annotations=_MUTATING)
    def offload_cancel(op_id: str) -> dict[str, Any]:
        """Cancel an operation between chunks. Nothing unverified is ever deleted; a partial restore is discarded."""
        return call("POST", f"/operations/{op_id}/cancel")

    @server.tool(annotations=_READ_ONLY)
    def offload_status(stable_ids: list[str]) -> dict[str, Any]:
        """Per-track offload state (``null`` = local, never offloaded) and the expiry of temporary restores."""
        return call("GET", "/tracks", params={"stable_ids": ",".join(stable_ids)})

    @server.tool(annotations=_MUTATING)
    def offload_sweep(dry_run: bool = False) -> dict[str, Any]:
        """Re-offload temporary restores past their expiry now. Never one on a deck, never during a gig."""
        return call("POST", "/sweep", json_body={"dry_run": dry_run})


__all__ = ["register_offload_tools"]
