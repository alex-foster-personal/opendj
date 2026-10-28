"""MCP energy tools (MIKBF-05): MIK energy coverage and the backfill loop.

Split out of :mod:`apps.opendj_cli.mcp_server` (600-line ceiling), on the
pattern of :mod:`apps.opendj_cli.mcp_genre_tools`. Each tool is one call to the
running engine's ``/api/v1/energy`` routes, so the MCP, HTTP and CLI
(``python -m apps.mik backfill``) surfaces return the same documents.

The loop an agent follows: ``energy_status`` -> ``energy_backfill_plan`` ->
``energy_backfill_stage`` (live) -> Mixed In Key analyzes the copies ->
``energy_backfill_collect`` (live), and again until nothing is left to
analyze. No tool launches Mixed In Key: ``stage`` returns the command.
Rows counted ``would_gain_by_duplicate_sharing`` need no analysis:
``energy_backfill_share`` (live) copies a complete donor's energy to them.

``stage`` and ``collect`` are dry runs unless ``live`` is set. Deleting the
analyzed copies is gated like every destructive call
(``OPENDJ_MCP_ENABLE_DESTRUCTIVE``). A non-2xx answer is a tool error carrying
the engine's ``code``, never an empty result.

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
ENERGY_PATH = "/api/v1/energy"
# Reading a MIK store and matching it against the library takes seconds; a
# live stage copies up to a few hundred audio files.
PLAN_TIMEOUT_S = 120.0
COPY_TIMEOUT_S = 900.0


@dataclass(frozen=True)
class _Hooks:
    lock_path: Callable[[], Path | None]
    tool_error: Callable[[dict[str, Any]], ToolError]
    engine_not_running: Callable[[EngineNotRunning], ToolError]


def _call(hooks: _Hooks, method: str, path: str, *, json_body: Any, params: Any, timeout: float) -> Any:
    """One request to the engine's energy routes; a non-2xx answer is a tool error with its code."""
    try:
        origin = resolve_origin(hooks.lock_path())
    except EngineNotRunning as error:
        raise hooks.engine_not_running(error) from error
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.request(method, f"{origin.base_url}{ENERGY_PATH}{path}", json=json_body, params=params)
    except httpx.TransportError as error:
        raise hooks.tool_error(
            {"error": "engine_unreachable", "message": str(error), "origin": origin.base_url}
        ) from error
    try:
        body = response.json()
    except ValueError:
        if response.is_success:
            # A 2xx that is not JSON is engine or proxy corruption, never a result.
            raise hooks.tool_error(
                {"error": "engine_bad_response", "status_code": response.status_code, "text": response.text[:500]}
            ) from None
        body = {"text": response.text[:500]}
    if response.is_success and not isinstance(body, dict):
        raise hooks.tool_error({"error": "engine_bad_response", "status_code": response.status_code, "body": body})
    if not response.is_success:
        detail = body.get("detail") if isinstance(body, dict) else None
        code = engine_error_code(detail, response.status_code)
        raise hooks.tool_error({"error": code, "status_code": response.status_code, "detail": detail or body})
    return body


def register_energy_tools(
    server: MCPServer,
    *,
    lock_path: Callable[[], Path | None],
    tool_error: Callable[[dict[str, Any]], ToolError],
    engine_not_running: Callable[[EngineNotRunning], ToolError],
) -> None:
    """Register the ``energy_*`` tools on ``server``."""

    hooks = _Hooks(lock_path, tool_error, engine_not_running)

    def call(
        method: str, path: str, *, json_body: Any = None, params: Any = None, timeout: float = PLAN_TIMEOUT_S
    ) -> Any:
        return _call(hooks, method, path, json_body=json_body, params=params, timeout=timeout)

    @server.tool(annotations=_READ_ONLY)
    def energy_status(share_across_duplicates: bool = False) -> dict[str, Any]:
        """How many PRESENT tracks carry a Mixed In Key energy, and why the rest do not.

        The denominator is named in the result (``present``: tracks whose
        audio is on disk). Each present track is in exactly one bucket: with
        MIK energy, held by another source, would gain by unique match, would
        gain by duplicate sharing, conflicting, or needs a fresh analysis.
        """
        return call("GET", "/status", params={"share_across_duplicates": share_across_duplicates})

    @server.tool(annotations=_READ_ONLY)
    def energy_backfill_plan(
        limit: int | None = None, store: str | None = None, share_across_duplicates: bool = False
    ) -> dict[str, Any]:
        """The files that need a fresh Mixed In Key analysis, one per recording. Reads only.

        ``store`` is a COPY of a MIK store that has not been loaded yet: tracks
        it would settle are counted and left out. ``share_across_duplicates``
        also treats same artist + title with durations within 1 s as one
        recording, so duplicates are analyzed once.
        """
        body = {"limit": limit, "store": store, "share_across_duplicates": share_across_duplicates}
        return call("POST", "/backfill/plan", json_body=body)

    @server.tool(annotations=_MUTATING)
    def energy_backfill_stage(
        limit: int | None = None,
        live: bool = False,
        store: str | None = None,
        share_across_duplicates: bool = False,
        min_free_bytes: int | None = None,
    ) -> dict[str, Any]:
        """Copy the next planned files into a new batch folder. A dry run unless ``live``.

        Never touches the original files and never launches Mixed In Key: the
        result carries ``open_command``, the exact command that queues the
        copies for analysis. It refuses when free disk would drop below the
        floor: ``min_free_bytes`` if given, else the library's stored setting.
        """
        body = {
            "limit": limit,
            "live": live,
            "store": store,
            "share_across_duplicates": share_across_duplicates,
            "min_free_bytes": min_free_bytes,
        }
        return call("POST", "/backfill/stage", json_body=body, timeout=COPY_TIMEOUT_S)

    @server.tool(annotations=_DESTRUCTIVE)
    def energy_backfill_collect(
        batch_dir: str,
        store: str,
        live: bool = False,
        delete_copies: bool = False,
        share_across_duplicates: bool = False,
        copy_store_first: bool = False,
    ) -> dict[str, Any]:
        """Load a batch's energy and energy series from a COPY of the MIK store. A dry run unless ``live``.

        ``batch_dir`` is the folder a live stage returned. ``store`` must be a
        copy of the MIK store, or MIK's own file with ``copy_store_first``.
        Fields the equivalence gate has not passed come back under
        ``blocked_by_gate`` and are not written. ``delete_copies`` removes
        only copies inside the batch folder whose energy was loaded.
        """
        if delete_copies and not destructive_enabled():
            raise tool_error(
                {
                    "error": "destructive_blocked",
                    "message": f"set {ENABLE_DESTRUCTIVE_ENV}=1 to delete the analyzed copies",
                }
            )
        body = {
            "batch_dir": batch_dir,
            "store": store,
            "live": live,
            "delete_copies": delete_copies,
            "share_across_duplicates": share_across_duplicates,
            "copy_store_first": copy_store_first,
        }
        return call("POST", "/backfill/collect", json_body=body, timeout=COPY_TIMEOUT_S)

    @server.tool(annotations=_MUTATING)
    def energy_backfill_share(
        live: bool = False, share_across_duplicates: bool = False, store: str | None = None
    ) -> dict[str, Any]:
        """Give a complete donor's MIK energy and series to its empty present duplicates.

        A dry run unless ``live``. Never overwrites a row with its own energy,
        and writes nothing for a group whose donors disagree (``conflicting``).
        ``store`` is a COPY of an unloaded MIK store: rows it would settle are
        held (``held_for_pending_analysis``). A second live run writes nothing.
        """
        body = {"live": live, "share_across_duplicates": share_across_duplicates, "store": store}
        return call("POST", "/backfill/share", json_body=body, timeout=COPY_TIMEOUT_S)


__all__ = ["register_energy_tools"]
