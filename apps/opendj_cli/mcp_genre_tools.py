"""MCP genre tools (GENRE-06): read, attach, infer, find similar, write the file tag.

Split out of :mod:`apps.opendj_cli.mcp_server` (600-line ceiling). Each tool is
one call to the running engine's ``/api/v1/genres`` routes, so the MCP, HTTP and
CLI (``python -m apps.genre_infer``) surfaces return the same documents.

A non-2xx answer is a tool error carrying the engine's ``code`` (for example
``genre_key_missing`` naming the missing key), never an empty result.
Writing a genre into an audio file is gated like every destructive call
(``OPENDJ_MCP_ENABLE_DESTRUCTIVE``).

-Claude
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from apps.opendj_cli.mcp_safety import ENABLE_DESTRUCTIVE_ENV, destructive_enabled, engine_error_code
from apps.opendj_cli.mcp_sdk import MCPServer, ToolAnnotations, ToolError
from apps.opendj_cli.origin import EngineNotRunning, resolve_origin

_READ_ONLY = ToolAnnotations(read_only_hint=True)
_MUTATING = ToolAnnotations(read_only_hint=False, destructive_hint=False)
_DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True)
GENRES_PATH = "/api/v1/genres"
# Inference of 500 tracks is a few minutes of network calls.
INFER_TIMEOUT_S = 600.0
DEFAULT_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class _Hooks:
    lock_path: Callable[[], Path | None]
    tool_error: Callable[[dict[str, Any]], ToolError]
    engine_not_running: Callable[[EngineNotRunning], ToolError]


def _call(hooks: _Hooks, method: str, path: str, *, json_body: Any, params: Any, timeout: float) -> Any:
    """One request to the engine's genre routes; a non-2xx answer is a tool error with its code."""
    try:
        origin = resolve_origin(hooks.lock_path())
    except EngineNotRunning as error:
        raise hooks.engine_not_running(error) from error
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.request(method, f"{origin.base_url}{GENRES_PATH}{path}", json=json_body, params=params)
    except httpx.TransportError as error:
        raise hooks.tool_error(
            {"error": "engine_unreachable", "message": str(error), "origin": origin.base_url}
        ) from error
    try:
        body = response.json()
    except ValueError:
        body = {"text": response.text[:500]}
    if not response.is_success:
        detail = body.get("detail") if isinstance(body, dict) else None
        code = engine_error_code(detail, response.status_code)
        raise hooks.tool_error({"error": code, "status_code": response.status_code, "detail": detail or body})
    return body


def register_genre_tools(
    server: MCPServer,
    *,
    lock_path: Callable[[], Path | None],
    tool_error: Callable[[dict[str, Any]], ToolError],
    engine_not_running: Callable[[EngineNotRunning], ToolError],
) -> None:
    """Register the ``genres_*`` tools on ``server``."""

    hooks = _Hooks(lock_path, tool_error, engine_not_running)

    def call(
        method: str, path: str, *, json_body: Any = None, params: Any = None, timeout: float = DEFAULT_TIMEOUT_S
    ) -> Any:
        return _call(hooks, method, path, json_body=json_body, params=params, timeout=timeout)

    @server.tool(annotations=_READ_ONLY)
    def genres_get(stable_ids: list[str]) -> dict[str, Any]:
        """Every provider's genre finding plus the ONE chosen genre the library shows, per track.

        ``chosen.provider`` names where the shown genre came from (``user``,
        ``beatport``, ``odj-fused``, ``library-tag``...); ``precedence`` is the
        configured order that picked it. Duplicates of a track share one result:
        ``duplicates`` lists a track's other copies, and a finding's
        ``stable_id`` names the copy that holds it.
        """
        return call("GET", "", params=[("ids", sid) for sid in stable_ids])

    @server.tool(annotations=_READ_ONLY)
    def genres_status() -> dict[str, Any]:
        """Whether inference can run (missing keys are named), the taxonomy, config and counts per provider."""
        return call("GET", "/status")

    @server.tool(annotations=_MUTATING)
    def genres_attach(
        stable_id: str,
        provider: str,
        genre: str,
        confidence: float,
        source: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Attach a genre finding from an outside source (for example ``provider='beatport'``).

        ``source`` says where it came from (a URL or a name). The finding is
        shown only if ``provider`` is in the configured precedence.
        """
        body = {
            "stable_id": stable_id,
            "provider": provider,
            "genre": genre,
            "confidence": confidence,
            "source": source,
            "note": note,
        }
        return call("POST", "/findings", json_body=body)

    @server.tool(annotations=_MUTATING)
    def genres_infer(stable_ids: list[str], force: bool = False) -> dict[str, Any]:
        """Run genre inference now for up to 500 tracks; cached by request digest unless ``force``.

        Costs a web search per uncached track plus two small model calls; the
        result reports the measured cost. Duplicates of a track are asked once:
        the other copies report ``shared_from`` and cost nothing.
        """
        return call("POST", "/infer", json_body={"stable_ids": stable_ids, "force": force}, timeout=INFER_TIMEOUT_S)

    @server.tool(annotations=_MUTATING)
    def genres_set_key(name: str, value: str) -> dict[str, Any]:
        """Store OPENROUTER_API_KEY or SERPER_API_KEY in the engine's macOS Keychain so inference runs from a Finder launch."""
        return call("PUT", f"/keys/{name}", json_body={"value": value})

    @server.tool(annotations=_MUTATING)
    def genres_enqueue_missing() -> dict[str, Any]:
        """Queue every present track with no inferred genre yet on the background genre lane (one job per duplicate group)."""
        return call("POST", "/enqueue", json_body={"all_missing": True})

    @server.tool(annotations=_READ_ONLY)
    def genres_similar(stable_id: str, limit: int = 20) -> dict[str, Any]:
        """Tracks whose genre probability vector is closest (cosine) to this one's; never its own duplicates."""
        return call("GET", f"/{stable_id}/similar", params={"limit": limit})

    @server.tool(annotations=_DESTRUCTIVE)
    def genres_write_tag(stable_id: str) -> dict[str, Any]:
        """Write the chosen genre into the track's audio file (backup + undo journal; config-gated)."""
        if not destructive_enabled():
            raise tool_error(
                {"error": "destructive_blocked", "message": f"set {ENABLE_DESTRUCTIVE_ENV}=1 to write audio-file tags"}
            )
        return call("POST", "/tags/write", json_body={"stable_id": stable_id})


__all__ = ["register_genre_tools"]
