"""MCP Rekordbox ANALYSE-ME tools (RBXAN-8322c): plan, apply, adopt.

Each tool is one call to the running engine's ``/api/v1/rekordbox/analyse``
routes, so MCP, HTTP and CLI (``python -m apps.rekordbox_analyse``) return the
same documents. ``rekordbox_analyse_apply`` writes into the user's Rekordbox
library, so it is gated like every destructive call
(``OPENDJ_MCP_ENABLE_DESTRUCTIVE``) on top of the engine's own guards.

-Claude
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from apps.opendj_cli.mcp_safety import ENABLE_DESTRUCTIVE_ENV, destructive_enabled, engine_error_code
from apps.opendj_cli.mcp_sdk import MCPServer, ToolAnnotations, ToolError
from apps.opendj_cli.origin import EngineNotRunning, resolve_origin

_READ_ONLY = ToolAnnotations(read_only_hint=True)
_MUTATING = ToolAnnotations(read_only_hint=False, destructive_hint=False)
_DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True)
ANALYSE_PATH = "/api/v1/rekordbox/analyse"
#: A plan decrypts a copy of master.db; adopt re-ingests the whole library.
TIMEOUT_S = 300.0


def register_rekordbox_analyse_tools(
    server: MCPServer,
    *,
    lock_path: Callable[[], Path | None],
    tool_error: Callable[[dict[str, Any]], ToolError],
    engine_not_running: Callable[[EngineNotRunning], ToolError],
) -> None:
    """Register the ``rekordbox_analyse_*`` tools on ``server``."""

    def call(method: str, path: str, json_body: Any = None) -> Any:
        try:
            origin = resolve_origin(lock_path())
        except EngineNotRunning as error:
            raise engine_not_running(error) from error
        try:
            with httpx.Client(timeout=TIMEOUT_S) as client:
                response = client.request(method, f"{origin.base_url}{ANALYSE_PATH}{path}", json=json_body)
        except httpx.TransportError as error:
            raise tool_error(
                {"error": "engine_unreachable", "message": str(error), "origin": origin.base_url}
            ) from error
        try:
            body = response.json()
        except ValueError:
            body = {"text": response.text[:500]}
        if not response.is_success:
            detail = body.get("detail") if isinstance(body, dict) else None
            code = engine_error_code(detail, response.status_code)
            raise tool_error({"error": code, "status_code": response.status_code, "detail": detail or body})
        return body

    @server.tool(annotations=_READ_ONLY)
    def rekordbox_analyse_plan() -> dict[str, Any]:
        """What one write would do so Rekordbox can analyse every Open DJ track it has not analysed yet.

        Read-only. ``waiting`` is the count the app shows; ``add_paths`` join
        Rekordbox's collection; ``playlist_add_ids`` / ``playlist_remove_ids``
        change the ANALYSE-ME playlist; ``viva_status`` says whether the
        ``Viva 26 - master`` playlist can take the Viva tracks;
        ``rekordbox_running`` lists Rekordbox processes that would block a
        write. Pass ``plan_sha`` to apply.
        """
        return call("GET", "/plan")

    @server.tool(annotations=_DESTRUCTIVE)
    def rekordbox_analyse_apply(plan_sha: str, cloud_sync_off_confirmed: bool) -> dict[str, Any]:
        """Write the confirmed plan into the user's Rekordbox library (backup, verify, auto-restore).

        Refuses (409, nothing written) unless Rekordbox and rekordboxAgent have
        quit, the plan is unchanged and the user confirmed Cloud Sync is off.
        """
        if not destructive_enabled():
            raise tool_error(
                {"error": "destructive_blocked", "message": f"set {ENABLE_DESTRUCTIVE_ENV}=1 to write into Rekordbox"}
            )
        return call("POST", "/apply", {"plan_sha": plan_sha, "cloud_sync_off_confirmed": cloud_sync_off_confirmed})

    @server.tool(annotations=_MUTATING)
    def rekordbox_analyse_adopt() -> dict[str, Any]:
        """Bring Rekordbox's analysis (BPM, key, rating, cues; not the beatgrid yet, RBXAN-8322g) into Open DJ; read-only on Rekordbox."""
        return call("POST", "/adopt")


__all__ = ["register_rekordbox_analyse_tools"]
