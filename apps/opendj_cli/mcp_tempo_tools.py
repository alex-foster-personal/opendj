"""MCP tempo-dispute tool (GRID-OCTAVE-02): pick our tempo or Mixed In Key's for one track.

The agent twin of the BPM hover card's "use the other tempo" button and of
``opendj track bpm-pick``: one call to ``PUT``/``DELETE
/api/v1/tracks/{id}/bpm-pick`` on the running engine. Rows and ``/anlz`` carry
``bpm_disputed`` and ``bpm_alternatives`` (``[{source, bpm}]``) to read first.

-Claude
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

import httpx

from apps.opendj_cli.mcp_safety import engine_error_code
from apps.opendj_cli.mcp_sdk import MCPServer, ToolAnnotations, ToolError
from apps.opendj_cli.origin import EngineNotRunning, resolve_origin

_MUTATING = ToolAnnotations(read_only_hint=False, destructive_hint=False)
TIMEOUT_S = 30.0


def register_tempo_tools(
    server: MCPServer,
    *,
    lock_path: Callable[[], Path | None],
    tool_error: Callable[[dict[str, Any]], ToolError],
    engine_not_running: Callable[[EngineNotRunning], ToolError],
) -> None:
    """Register ``track_bpm_pick`` on ``server``."""

    @server.tool(annotations=_MUTATING)
    def track_bpm_pick(stable_id: str, source: Literal["own", "mik"] | None) -> dict[str, Any]:
        """Serve our tempo (``own``) or Mixed In Key's (``mik``) where they disagree; ``None`` forgets the pick.

        The pick is stored like a grid edit: the deck's grid and the library
        BPM both follow it, and ``bpm_disputed`` goes false. Read the track's
        ``bpm_alternatives`` first; on a track whose tempos agree it changes nothing.
        """
        try:
            origin = resolve_origin(lock_path())
        except EngineNotRunning as error:
            raise engine_not_running(error) from error
        url = f"{origin.base_url}/api/v1/tracks/{quote(stable_id, safe='')}/bpm-pick"
        try:
            with httpx.Client(timeout=TIMEOUT_S) as client:
                response = client.delete(url) if source is None else client.put(url, json={"source": source})
        except httpx.TransportError as error:
            raise tool_error({"error": "engine_unreachable", "message": str(error), "origin": origin.base_url}) from error
        body = response.json()
        if not response.is_success:
            detail = body.get("detail") if isinstance(body, dict) else None
            raise tool_error({"error": engine_error_code(detail, response.status_code), "status_code": response.status_code, "detail": detail})
        return body


__all__ = ["register_tempo_tools"]
