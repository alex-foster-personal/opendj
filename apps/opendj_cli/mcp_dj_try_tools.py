"""MCP transition try-er tools (TRYER-12), the twins of ``opendj dj try``.

* ``try_transitions``: renders and scores offline and writes a job row; it
  never touches the live mix, so it is mutating but not destructive.
* ``pick_transition`` / ``undo_transition_pick``: If-Match guarded writes of
  the choice, each reversible by the other: mutating, not destructive.

Registered by :func:`apps.opendj_cli.mcp_dj_tools.register_dj_tools` with the
same error mappers, so a refusal reads the same as every other AutoDJ tool's.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from apps.opendj_cli.dj_service import DjError
from apps.opendj_cli.dj_try_service import DjTryService
from apps.opendj_cli.mcp_sdk import MCPServer, ToolAnnotations, ToolError
from apps.opendj_cli.origin import EngineIdentityMismatch, EngineNotRunning, resolve_verified_origin

_MUTATING = ToolAnnotations(read_only_hint=False, destructive_hint=False)


def register_try_tools(
    server: MCPServer,
    *,
    lock_path: Callable[[], Path | None],
    tool_error: Callable[[dict[str, Any]], ToolError],
    engine_not_running: Callable[[EngineNotRunning], ToolError],
    engine_identity_mismatch: Callable[[EngineIdentityMismatch], ToolError],
) -> None:
    """Register the try-er tools on ``server``."""

    def _call(run: Callable[[DjTryService], dict[str, Any]]) -> dict[str, Any]:
        try:
            return run(DjTryService(origin=resolve_verified_origin(lock_path())))
        except EngineIdentityMismatch as error:
            raise engine_identity_mismatch(error) from error
        except EngineNotRunning as error:
            raise engine_not_running(error) from error
        except DjError as error:
            raise tool_error(error.document()) from error

    @server.tool(annotations=_MUTATING)
    def try_transitions(out_stable_id: str, in_stable_id: str, n: int = 6, context_bars: int = 4) -> dict[str, Any]:
        """Render, score and rank n candidate transitions from one track into another,
        offline (never on the live mix). Returns a job_id, an etag, and per candidate:
        plan, plan_sha256, style, bars, score (null when fewer than 4 of 7 core factors
        were measured, 0 when a veto fired), measured {k, n}, unknown factors with
        reasons, vetoes, metrics and a render_url to audition. Pick with pick_transition."""
        return _call(lambda service: service.try_transitions(out_stable_id, in_stable_id, n, context_bars))

    @server.tool(annotations=_MUTATING)
    def pick_transition(job_id: str, candidate_id: str, etag: str) -> dict[str, Any]:
        """Record the chosen candidate of a try_transitions job: stores its plan, plan
        sha256, scorer version and the rejected candidate ids. etag is the one on the
        job document you reviewed: a job changed since is refused (etag_mismatch), and a
        job that already has a pick is refused (already_picked; undo first)."""
        return _call(lambda service: service.pick(job_id, candidate_id, etag))

    @server.tool(annotations=_MUTATING)
    def undo_transition_pick(job_id: str, etag: str) -> dict[str, Any]:
        """Remove the pick of a try_transitions job, against the etag of the job document you
        reviewed (refused when it has no pick, or changed since)."""
        return _call(lambda service: service.undo(job_id, etag))
