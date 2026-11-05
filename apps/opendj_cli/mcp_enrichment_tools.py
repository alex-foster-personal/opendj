"""MCP ingest enrichment tools (ENRICH-09, LIBM-189): batch status, rerun, store cover consent.

Each tool is one call to the running engine's ``/api/v1/ingest/enrichment``
routes, so MCP, HTTP and ``opendj enrichment`` return the same documents. A
non-2xx answer is a tool error carrying the engine's ``code``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from apps.opendj_cli.mcp_safety import engine_error_code
from apps.opendj_cli.mcp_sdk import MCPServer, ToolAnnotations, ToolError
from apps.opendj_cli.origin import EngineNotRunning, resolve_origin

ENRICHMENT_PATH = "/api/v1/ingest/enrichment"
SUGGESTIONS_PATH = "/api/v1/artwork/suggestions"
TIMEOUT_S = 30.0
DECIDE_TIMEOUT_S = 120.0
_READ_ONLY = ToolAnnotations(read_only_hint=True)
_MUTATING = ToolAnnotations(read_only_hint=False, destructive_hint=False)


def register_enrichment_tools(
    server: MCPServer,
    *,
    lock_path: Callable[[], Path | None],
    tool_error: Callable[[dict[str, Any]], ToolError],
    engine_not_running: Callable[[EngineNotRunning], ToolError],
) -> None:
    """Register the ``ingest_enrichment_*`` tools on ``server``."""

    def call(
        method: str, path: str, json_body: Any = None, *, root: str = ENRICHMENT_PATH, timeout: float = TIMEOUT_S
    ) -> Any:
        try:
            origin = resolve_origin(lock_path())
        except EngineNotRunning as error:
            raise engine_not_running(error) from error
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.request(method, f"{origin.base_url}{root}{path}", json=json_body)
        except httpx.TransportError as error:
            raise tool_error({"error": "engine_unreachable", "message": str(error)}) from error
        body = response.json()
        if not response.is_success:
            detail = body.get("detail") if isinstance(body, dict) else None
            raise tool_error(
                {
                    "error": engine_error_code(detail, response.status_code),
                    "status_code": response.status_code,
                    "detail": detail or body,
                }
            )
        return body

    @server.tool(annotations=_READ_ONLY)
    def ingest_enrichment_status(batch_id: str | None = None) -> dict[str, Any]:
        """What each recent import batch still lacks, lane by lane in priority order.

        Lanes run in ``lane_priority`` order: tags, waveform, beatgrid, key and
        loudness for every track of a batch, then artwork (which waits until
        all of them are settled). ``warnings`` names any lane that cannot run
        here and where to set what is missing. With ``batch_id``, every
        track's state and reason per lane is included.
        """
        return call("GET", "" if batch_id is None else f"/{quote(batch_id, safe='')}")

    @server.tool(annotations=_MUTATING)
    def ingest_enrichment_rerun(batch_id: str | None = None, playlist_id: str | None = None) -> dict[str, Any]:
        """Enrich one import batch or one playlist again: forget its failures and cover misses, move it to the front.

        Name exactly one of ``batch_id`` or ``playlist_id``.
        """
        return call("POST", "/rerun", {"batch_id": batch_id, "playlist_id": playlist_id})

    @server.tool(annotations=_READ_ONLY)
    def artwork_suggestions_list() -> dict[str, Any]:
        """Covers Deezer or iTunes has for tracks MusicBrainz lacked, waiting on the user's consent.

        Nothing from a store is saved until accepted: their terms do not allow
        caching their content, so only accept covers for tracks the user owns
        and bought on that platform (``note``).
        """
        return call("GET", "", root=SUGGESTIONS_PATH)

    @server.tool(annotations=_MUTATING)
    def artwork_suggestions_decide(
        accept: list[str] | None = None,
        skip: list[str] | None = None,
        accept_all: bool = False,
        skip_all: bool = False,
    ) -> dict[str, Any]:
        """Save (``accept``) or never suggest again (``skip``) store covers, per stable_id or all.

        Only on the user's say-so: they must own the tracks, bought on that store.
        """
        body = {"accept": accept or [], "skip": skip or [], "accept_all": accept_all, "skip_all": skip_all}
        return call("POST", "/decide", body, root=SUGGESTIONS_PATH, timeout=DECIDE_TIMEOUT_S)


__all__ = ["register_enrichment_tools"]
