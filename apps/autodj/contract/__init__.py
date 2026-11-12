"""The public surface of ``apps.autodj`` (ADR-NEW-module-contracts).

Code outside ``apps.autodj`` imports only this package (and its submodules).
The contract imports only the standard library, pydantic and other contracts.
"""

from __future__ import annotations

from .models import (
    ANCHOR_LEAD_BARS,
    ENERGY_UP,
    INTENTS,
    SHOWY,
    SMOOTH,
    STYLE_CAP,
    VARIETY_WINDOW,
    VOCALS_CLEAN,
    WIND_DOWN,
    BassEntry,
    ChooserCandidate,
    HotCue,
    KeySegment,
    PairNotFound,
    PairTransition,
    PlanError,
    PlanEvent,
    ProfileError,
    RenderLayout,
    RenderPlanError,
    Section,
    Side,
    StyleBars,
    StyleParamsWire,
    StyleSummary,
    TrackProfile,
    TransitionPlan,
    ValidationResult,
)
from .service import AutoDjService

CONTRACT_VERSION = "autodj/2"

__all__ = [
    "ANCHOR_LEAD_BARS",
    "CONTRACT_VERSION",
    "ENERGY_UP",
    "INTENTS",
    "SHOWY",
    "SMOOTH",
    "STYLE_CAP",
    "VARIETY_WINDOW",
    "VOCALS_CLEAN",
    "WIND_DOWN",
    "AutoDjService",
    "BassEntry",
    "ChooserCandidate",
    "HotCue",
    "KeySegment",
    "PairNotFound",
    "PairTransition",
    "PlanError",
    "PlanEvent",
    "ProfileError",
    "RenderLayout",
    "RenderPlanError",
    "Section",
    "Side",
    "StyleBars",
    "StyleParamsWire",
    "StyleSummary",
    "TrackProfile",
    "TransitionPlan",
    "ValidationResult",
]
