"""The composition root for ``apps.autodj`` (ADR-NEW-module-contracts).

Delivery layers call :func:`build_service` and hold the result as the
contract's :class:`~apps.autodj.contract.service.AutoDjService` Protocol. The
planner names a delivery layer drives directly (plan, compile, profile, scoring
and windows) are re-exported here, so no delivery layer reaches a planner module.

The re-exports are a stopgap, not the pattern: they widen the surface the contract narrows, and
(private path) owes the move of those operations behind ``AutoDjService``.
"""

from __future__ import annotations

from apps.autodj import styles
from apps.autodj.compile import compile_plan, control_command
from apps.autodj.contract.models import StyleSummary
from apps.autodj.contract.service import AutoDjService
from apps.autodj.live import ANCHOR_LEAD_BARS, lead_short, live_bar, with_in_track
from apps.autodj.plan import (
    PlanError,
    TransitionPlan,
    Validation,
    first_dispatch_bar,
    live_child,
    parse_plan,
    validate_plan,
)
from apps.autodj.profile import ProfileError, TrackProfile, profile_from_payloads
from apps.autodj.propose import propose_transition
from apps.autodj.scoring import score_pair, score_windows
from apps.autodj.windows import ROLES, MixWindow, mix_windows, window_at

__all__ = [
    "ANCHOR_LEAD_BARS",
    "ROLES",
    "MixWindow",
    "PlanError",
    "ProfileError",
    "TrackProfile",
    "TransitionPlan",
    "Validation",
    "build_service",
    "compile_plan",
    "control_command",
    "first_dispatch_bar",
    "lead_short",
    "live_bar",
    "live_child",
    "mix_windows",
    "parse_plan",
    "profile_from_payloads",
    "propose_transition",
    "score_pair",
    "score_windows",
    "validate_plan",
    "window_at",
    "with_in_track",
]


class _Service:
    def list_styles(self) -> list[StyleSummary]:
        return [StyleSummary.model_validate(doc) for doc in styles.list_styles()]


def build_service() -> AutoDjService:
    return _Service()
