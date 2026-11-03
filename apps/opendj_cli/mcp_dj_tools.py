"""MCP AutoDJ tools (AGENT-29): the canon doc's agent tool surface.

``(private path)`` ("Proposed tool surface
for agents") names these tools; this module registers the ones the app can
answer truthfully today, each a thin call into :class:`DjService` so the
``opendj dj`` CLI answers the same way:

* read: ``get_mix_state``, ``get_track_profile``, ``get_mix_windows``,
  ``score_pair``, ``suggest_next``, ``list_verbs``
* plan: ``list_transition_styles``, ``propose_transition``, ``validate_plan``
* execute: ``schedule_plan``, ``amend_plan``
* try-er: ``try_transitions``, ``pick_transition``, ``undo_transition_pick``
  (:mod:`mcp_dj_try_tools`, TRYER-12)

Not registered yet, on purpose: ``render_preview`` (the Rust renderer has no
metrics module and no stems) and ``log_transition_outcome``. A tool that
cannot measure would have to answer with something, and it must not.

Primitive control (load, play, EQ, faders, every bus verb) stays on the
existing ``command`` tool; ``list_verbs`` is how an agent discovers it.

Split out of :mod:`mcp_server` to keep that module under the 600-line ceiling;
the callables are injected so this module does not import the server.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from apps.autodj.http import build_service
from apps.opendj_cli.dj_service import DjError, DjService
from apps.opendj_cli.mcp_dj_try_tools import register_try_tools
from apps.opendj_cli.mcp_sdk import MCPServer, ToolAnnotations, ToolError
from apps.opendj_cli.origin import (
    EngineIdentityMismatch,
    EngineNotRunning,
    EngineOrigin,
    resolve_verified_origin,
)
from apps.opendj_cli.verbs import describe_verbs

_READ_ONLY = ToolAnnotations(read_only_hint=True)
_MUTATING = ToolAnnotations(read_only_hint=False)
_DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True)


def _plan_argument(plan: dict[str, Any] | str) -> dict[str, Any]:
    """A plan arrives as an object, or as the JSON text of one."""
    if isinstance(plan, str):
        try:
            parsed = json.loads(plan)
        except ValueError as error:
            raise DjError("plan_invalid", f"plan is not JSON: {error}") from error
        if not isinstance(parsed, dict):
            raise DjError("plan_invalid", "plan must be a JSON object")
        return parsed
    return plan


def register_dj_tools(
    server: MCPServer,
    *,
    lock_path: Callable[[], Path | None],
    tool_error: Callable[[dict[str, Any]], ToolError],
    engine_not_running: Callable[[EngineNotRunning], ToolError],
    engine_identity_mismatch: Callable[[EngineIdentityMismatch], ToolError],
) -> None:
    """Register the AutoDJ tools on ``server``."""
    register_try_tools(
        server,
        lock_path=lock_path,
        tool_error=tool_error,
        engine_not_running=engine_not_running,
        engine_identity_mismatch=engine_identity_mismatch,
    )
    services: dict[tuple[str, str | None, int | None], DjService] = {}

    def _service() -> DjService:
        # One service (and so one profile cache) per engine BOOT: a relaunched
        # engine starts with a fresh cache even on the same port, so analysis or
        # library changes made across the restart never serve stale profiles.
        # The pid stands in for an engine whose lock carries no boot id.
        origin: EngineOrigin = resolve_verified_origin(lock_path())
        key = (origin.base_url, origin.boot_id, origin.pid)
        if key not in services:
            services.clear()
            services[key] = DjService(origin=origin)
        return services[key]

    def _call(run: Callable[[DjService], dict[str, Any]]) -> dict[str, Any]:
        try:
            return run(_service())
        except EngineIdentityMismatch as error:
            raise engine_identity_mismatch(error) from error
        except EngineNotRunning as error:
            raise engine_not_running(error) from error
        except DjError as error:
            raise tool_error(error.document()) from error

    @server.tool(annotations=_READ_ONLY)
    def get_mix_state() -> dict[str, Any]:
        """What is playing, per deck: track, playing/audible, BPM, key, mixer strip,
        and the bar, bars left and current section (from the track's analysis).
        Also the master deck, crossfader, AutoPlay state and the running plan."""
        return _call(lambda service: service.mix_state())

    @server.tool(annotations=_READ_ONLY)
    def get_track_profile(stable_id: str) -> dict[str, Any]:
        """One track's compact musical facts: BPM, key, bars, grid trust, labelled
        sections (intro/build/drop/breakdown/outro...) with relative energy, bass
        and vocal bars, key segments and hot cues. Never a full beatgrid."""
        return _call(lambda service: service.track_profile(stable_id))

    @server.tool(annotations=_READ_ONLY)
    def get_mix_windows(stable_id: str, role: str, limit: int = 5) -> dict[str, Any]:
        """Ranked places to leave (role "out") or enter (role "in") a track: start
        bar, the most bars a blend can use there, section label, vocal-free bars,
        the next peak bar (for "in"), a score and the reason."""
        return _call(lambda service: service.mix_windows(stable_id, role, limit))

    @server.tool(annotations=_READ_ONLY)
    def score_pair(
        out_stable_id: str,
        in_stable_id: str,
        out_start_bar: int | None = None,
        in_start_bar: int | None = None,
        overlap_bars: int | None = None,
    ) -> dict[str, Any]:
        """Score mixing one track into another at given windows (or the best ones):
        key distance at those bars, tempo gap and whether Beat Sync can close it,
        vocal and bass clashes in the overlap, energy step, with reasons."""
        return _call(
            lambda service: service.score_pair(
                out_stable_id, in_stable_id, out_start_bar, in_start_bar, overlap_bars
            )
        )

    @server.tool(annotations=_READ_ONLY)
    def suggest_next(
        stable_id: str | None = None,
        session_ids: list[str] | None = None,
        top_n: int = 10,
    ) -> dict[str, Any]:
        """Ranked next tracks for the given track (default: the master deck's),
        from the DJ copilot, with rationale tags and numbers."""
        return _call(lambda service: service.suggest_next(stable_id, session_ids, top_n))

    @server.tool(annotations=_READ_ONLY)
    def list_verbs() -> dict[str, Any]:
        """Every primitive the command tool accepts (load, play, eq, fader,
        crossfader, tempo, loops, hot cues, stems...), with its arguments and
        whether it can be ramped. Use them via command(verb=..., args=[...])."""
        return {"verbs": describe_verbs()}

    @server.tool(annotations=_READ_ONLY)
    def list_transition_styles() -> dict[str, Any]:
        """The transition style catalog: name, summary, bar range, whether it
        needs beatmatching, the controls it moves, when to use it, and whether
        the app can perform it today (with the reason when not). ``params`` is a
        parametric style's settings (volume lane, fade_shape, swap_position,
        filter, stems, min_body_fade_bars, pre_hit_db), null for a fixed
        style; ``spotify`` names the Spotify transition it mirrors, or null."""
        return {"styles": [s.model_dump(mode="json") for s in build_service().list_styles()]}

    @server.tool(annotations=_READ_ONLY)
    def propose_transition(
        in_stable_id: str,
        intent: str | None = None,
        out_deck: int | None = None,
        in_deck: int | None = None,
        top: int = 3,
    ) -> dict[str, Any]:
        """The top complete transition plans from the live outgoing deck (default:
        master) into a track, each validated and scored with a rationale. intent
        is free text ("keep the energy up", "wind down", "smooth", "showy",
        "keep vocals clean"); the parsed intent is returned. Deterministic: pass
        a proposal's plan to schedule_plan, or edit it first."""
        return _call(
            lambda service: service.propose(
                in_stable_id, out_deck=out_deck, in_deck=in_deck, intent=intent, top=top
            )
        )

    @server.tool(annotations=_READ_ONLY)
    def validate_plan(plan: dict[str, Any] | str) -> dict[str, Any]:
        """Check a TransitionPlan against both tracks' analysis and the live mix:
        errors (it would not be scheduled), warnings, and clash metrics (vocal and
        bass overlap bars, tempo gap, key distance, silent bars)."""
        return _call(lambda service: service.validate(_plan_argument(plan)))

    @server.tool(annotations=_DESTRUCTIVE)
    def schedule_plan(plan: dict[str, Any] | str) -> dict[str, Any]:
        """Perform a TransitionPlan: loads the incoming track on an idle deck if
        needed, validates against the live mix, and arms the page's executor,
        which fires every event on the outgoing deck's bars. Returns a plan_id at
        once; watch it with get_mix_state (agent_plan). While it is pending,
        agent_plan.overlay holds the curves drawn on each deck as track_ms points,
        and overlay.anchor_moved says the incoming deck left the planned cue: the
        plan still runs from there, so abort and schedule again to fit the new
        cue. A plan only continues a
        set that is already playing. In a muted agent session (the default;
        OPENDJ_AGENT_AUDIO=live turns it off) the plan mutes the master first.
        ``notes`` lists what the compiler decided on your behalf (for example an
        outgoing fader it could not read, so left uncapped); the order carries
        it as a required member (ADR-NEW-plan-order-notes)."""
        return _call(lambda service: service.schedule(_plan_argument(plan)))

    @server.tool(annotations=_MUTATING)
    def amend_plan(plan_id: str, action: str, bars: int | None = None) -> dict[str, Any]:
        """Change a scheduled or running plan: "abort" (stop where it is),
        "finish_now" (apply every remaining step at once), or "extend" with bars
        (push every step that has not fired yet later)."""
        return _call(lambda service: service.amend(plan_id, action, bars))
