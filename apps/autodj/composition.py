"""The body of ``apps.autodj.http``, the AutoDJ composition root (ADR-NEW-module-contracts).

Nothing outside this package imports this module: delivery layers and composition roots import
``apps.autodj.http``, which loads this module on first use of a name it re-exports. It lives apart
so that importing ``http`` for the try-picks DDL alone pulls in no third-party package (see that
module's docstring). The planner re-exports below are a stopgap, not the pattern: they widen the
surface the contract narrows, and (private path) owes the move of those operations behind
``AutoDjService``.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import JsonValue

from apps.autodj import styles, try_picks
from apps.autodj.capture import ClosedMix, TransitionCapture, in_bars
from apps.autodj.choose import choose_for_set
from apps.autodj.compile import compile_plan, control_command
from apps.autodj.contract.models import StyleSummary, ValidationResult
from apps.autodj.contract.service import AutoDjService
from apps.autodj.live import ANCHOR_LEAD_BARS, heard_on_deck, lead_short, live_bar, with_in_track
from apps.autodj.lookahead import Decision, decide
from apps.autodj.notation import ScoreError, looks_like_score, print_score, render_grid, score_document
from apps.autodj.pair_memory import (
    PairNotFound,
    PairTransition,
    clear_pair_transition,
    get_pair_transition,
    last_decision,
    on_decks,
    record_played,
    save_decision,
    save_pair_transition,
    set_verdict,
)
from apps.autodj.plan import (
    PlanError,
    TransitionPlan,
    Validation,
    first_dispatch_bar,
    live_child,
    live_mapping,
    loop_engaged,
    parse_plan,
    surviving_transport,
    validate_plan,
)
from apps.autodj.profile import (
    ProfileError,
    TrackProfile,
    downbeats_of,
    heard_profile,
    profile_from_payloads,
    snap_to_downbeat,
)
from apps.autodj.propose import (
    match_live_trim_db,
    parse_intent,
    propose_parity_bytes,
    propose_transition,
    refresh_proposal_confidence,
)
from apps.autodj.render_plan import (
    RENDER_SAMPLE_RATE,
    RenderPlanError,
    compile_render_plan,
    effective_bars,
    layout_of,
    renders_at_start,
)
from apps.autodj.scoring import score_pair, score_windows
from apps.autodj.setscore import (
    MirrorError,
    SetPlan,
    check_mirror,
    next_transition,
    parse_set,
    plan_set,
    print_set_score,
    read_set,
)
from apps.autodj.simplify import SimplifyError, simplify
from apps.autodj.windows import ROLES, MixWindow, mix_windows, window_at

__all__ = [
    "ANCHOR_LEAD_BARS",
    "RENDER_SAMPLE_RATE",
    "ROLES",
    "ClosedMix",
    "Decision",
    "MirrorError",
    "MixWindow",
    "PairNotFound",
    "PairTransition",
    "PlanError",
    "ProfileError",
    "RenderPlanError",
    "ScoreError",
    "SetPlan",
    "SimplifyError",
    "TrackProfile",
    "TransitionCapture",
    "TransitionPlan",
    "Validation",
    "build_service",
    "check_mirror",
    "choose_for_set",
    "clear_pair_transition",
    "compile_plan",
    "compile_render_plan",
    "control_command",
    "decide",
    "downbeats_of",
    "effective_bars",
    "first_dispatch_bar",
    "get_pair_transition",
    "heard_on_deck",
    "heard_profile",
    "in_bars",
    "last_decision",
    "layout_of",
    "lead_short",
    "live_bar",
    "live_child",
    "live_mapping",
    "looks_like_score",
    "loop_engaged",
    "match_live_trim_db",
    "mix_windows",
    "next_transition",
    "on_decks",
    "parse_intent",
    "parse_plan",
    "parse_set",
    "plan_set",
    "print_score",
    "print_set_score",
    "profile_from_payloads",
    "propose_parity_bytes",
    "propose_transition",
    "read_set",
    "record_played",
    "refresh_proposal_confidence",
    "render_grid",
    "renders_at_start",
    "save_decision",
    "save_pair_transition",
    "score_document",
    "score_pair",
    "score_windows",
    "set_verdict",
    "simplify",
    "snap_to_downbeat",
    "try_picks",
    "validate_plan",
    "window_at",
    "with_in_track",
]


class _Service:
    def list_styles(self) -> list[StyleSummary]:
        return [StyleSummary.model_validate(doc) for doc in styles.list_styles()]

    choose_for_set = staticmethod(choose_for_set)
    compile_plan = staticmethod(compile_plan)
    heard_on_deck = staticmethod(heard_on_deck)
    lead_short = staticmethod(lead_short)
    live_bar = staticmethod(live_bar)
    with_in_track = staticmethod(with_in_track)
    on_decks = staticmethod(on_decks)
    live_mapping = staticmethod(live_mapping)
    parse_plan = staticmethod(parse_plan)

    @staticmethod
    def validate_plan(
        plan: TransitionPlan,
        out_profile: TrackProfile,
        in_profile: TrackProfile,
        *,
        live: Mapping[str, JsonValue] | None = None,
    ) -> ValidationResult:
        result = validate_plan(plan, out_profile, in_profile, live=live)
        return ValidationResult(ok=result.ok, errors=tuple(result.errors))

    match_live_trim_db = staticmethod(match_live_trim_db)
    parse_intent = staticmethod(parse_intent)
    propose_transition = staticmethod(propose_transition)
    profile_from_payloads = staticmethod(profile_from_payloads)
    compile_render_plan = staticmethod(compile_render_plan)
    layout_of = staticmethod(layout_of)
    effective_bars = staticmethod(effective_bars)
    renders_at_start = staticmethod(renders_at_start)
    surviving_transport = staticmethod(surviving_transport)


def build_service() -> AutoDjService:
    return _Service()
