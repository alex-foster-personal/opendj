"""The outside surface of ``apps.autodj``: the composition root (ADR-NEW-module-contracts).

Delivery layers call :func:`build_service` and hold the result as the contract's
:class:`~apps.autodj.contract.service.AutoDjService` Protocol; the planner names a delivery layer
drives directly are re-exported here too (the stopgap (private path) owes).

Standard library at import time, because the schema-drift gate imports this module for the
try-picks DDL entry point and may pull in no third-party package (scripts/quality_gate.py runs it
with no project dependencies; tests/quality/test_sync_drift_imports.py pins it). Every other name
lives in :mod:`apps.autodj.composition`, which imports pydantic, and loads from there on first
access through the module ``__getattr__`` below (the lazy re-export pattern of Scientific Python
SPEC 1). Type checkers read the ``TYPE_CHECKING`` import instead, so every name keeps its type.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from apps.autodj import try_picks
from apps.autodj.try_picks import TRY_PICKS_SCHEMA_SOURCE

if TYPE_CHECKING:
    from apps.autodj.composition import (
        ANCHOR_LEAD_BARS,
        RENDER_SAMPLE_RATE,
        ROLES,
        ClosedMix,
        Decision,
        MirrorError,
        MixWindow,
        PairNotFound,
        PairTransition,
        PlanError,
        ProfileError,
        RenderPlanError,
        ScoreError,
        SetPlan,
        SimplifyError,
        TrackProfile,
        TransitionCapture,
        TransitionPlan,
        Validation,
        build_service,
        check_mirror,
        choose_for_set,
        clear_pair_transition,
        compile_plan,
        compile_render_plan,
        control_command,
        decide,
        downbeats_of,
        effective_bars,
        first_dispatch_bar,
        get_pair_transition,
        heard_on_deck,
        heard_profile,
        in_bars,
        last_decision,
        layout_of,
        lead_short,
        live_bar,
        live_child,
        live_mapping,
        looks_like_score,
        match_live_trim_db,
        mix_windows,
        next_transition,
        on_decks,
        parse_intent,
        parse_plan,
        parse_set,
        plan_set,
        print_score,
        print_set_score,
        profile_from_payloads,
        propose_parity_bytes,
        propose_transition,
        read_set,
        record_played,
        refresh_proposal_confidence,
        render_grid,
        renders_at_start,
        save_decision,
        save_pair_transition,
        score_document,
        score_pair,
        score_windows,
        set_verdict,
        simplify,
        snap_to_downbeat,
        validate_plan,
        window_at,
        with_in_track,
    )

__all__ = [
    "ANCHOR_LEAD_BARS",
    "RENDER_SAMPLE_RATE",
    "ROLES",
    "TRY_PICKS_SCHEMA_SOURCE",
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


def __getattr__(name: str) -> object:
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from apps.autodj import composition

    value = getattr(composition, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(__all__)
