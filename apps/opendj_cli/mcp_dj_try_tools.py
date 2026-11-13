"""MCP transition try-er tools (TRYER-12), the twins of ``opendj dj try``.

* ``try_transitions``: renders and scores offline and writes a job row; it
  never touches the live mix, so it is mutating but not destructive.
* ``pick_transition`` / ``undo_transition_pick``: If-Match guarded writes of
  the choice, each reversible by the other: mutating, not destructive.
* ``get_transition_try``: read-only, the twin of ``opendj dj try show``: the
  current job document and its etag, so an agent can refresh a stale etag
  before a guarded pick or undo.
* ``get_transition_lanes``: read-only, the lane document the try-er panel
  draws for one kept candidate (``GET .../candidates/{cid}/lanes``), so an
  agent can review a render's waveforms and knob lines without the page.

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
_READ_ONLY = ToolAnnotations(read_only_hint=True)


def register_try_tools(
    server: MCPServer,
    *,
    lock_path: Callable[[], Path | None],
    tool_error: Callable[[dict[str, Any]], ToolError],
    engine_not_running: Callable[[EngineNotRunning], ToolError],
    engine_identity_mismatch: Callable[[EngineIdentityMismatch], ToolError],
    service_factory: Callable[[Path | None], DjTryService] | None = None,
) -> None:
    """Register the try-er tools on ``server``; ``service_factory`` is the tests' seam, as in ``dj_try_cli.run``."""

    def _service() -> DjTryService:
        lock = lock_path()
        return (
            service_factory(lock) if service_factory is not None else DjTryService(origin=resolve_verified_origin(lock))
        )

    def _call(run: Callable[[DjTryService], dict[str, Any]]) -> dict[str, Any]:
        try:
            return run(_service())
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

    @server.tool(annotations=_READ_ONLY)
    def get_transition_try(job_id: str) -> dict[str, Any]:
        """Read a try_transitions job as it is now: candidates, pick (or null) and the
        current etag, the same document as `opendj dj try show`. Read it again before
        pick_transition or undo_transition_pick when another client may have changed the
        job; a missing job is refused (job_not_found)."""
        return _call(lambda service: service.get_job(job_id))

    @server.tool(annotations=_READ_ONLY)
    def get_transition_lanes(job_id: str, candidate_id: str, columns: int = 800) -> dict[str, Any]:
        """Read the lanes the try-er panel draws for one candidate's kept render: per deck,
        waveform peaks (columns values, 0..1 of full scale), knob curves (fader, eq_low,
        filter as (seconds, value) points), playing spans and beats, all on the render clock.
        columns must be 1..4000 (invalid_columns otherwise); an unknown candidate is refused
        (candidate_not_found) and a render no longer on disk is render_missing."""
        return _call(lambda service: service.lanes(job_id, candidate_id, columns))
