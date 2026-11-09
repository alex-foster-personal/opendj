"""The outside surface of ``apps.transition_score``: the composition root (ADR-NEW-module-contracts).

Delivery layers and scripts call :func:`build_service` and hold the result as the contract's
:class:`~apps.transition_score.contract.service.TransitionScoreService` Protocol; the try-er, live
planner and lane names a delivery layer drives directly are re-exported here too (the same stopgap
(private path) owes for ``apps.autodj.http``).

The scorer and signal names load on first access through the module ``__getattr__`` below, because
``signals`` imports scipy at module top and the engine must boot without the ``analysis`` extra
(``tryer._audio_stack``). Type checkers read the ``TYPE_CHECKING`` import instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import apps.transition_score.tryer as _tryer
from apps.autodj.contract import AutoDjService
from apps.transition_score.contract import TransitionScoreService, TryResult
from apps.transition_score.lanes import MAX_COLUMNS, LaneError, Lanes, build_lanes
from apps.transition_score.live import (
    DeckNotPlannable,
    LiveAnswer,
    LiveRequest,
    PlanCache,
    plan_live,
    prepare_live,
)
from apps.transition_score.live import _bar_now as deck_bar_now
from apps.transition_score.live import _key as prepare_key
from apps.transition_score.live import _track as deck_track
from apps.transition_score.tryer import (
    PROFILE_POINTS,
    AudioUnavailable,
    EngineProfiles,
    EngineTrackSource,
    PairNotTryable,
    ProfileReader,
    TrackSource,
    TryError,
    incoming_on_render_clock,
    outgoing_natural,
    rendered_steps,
    shared_start_out_bar,
    try_pair,
)

if TYPE_CHECKING:
    from apps.transition_score.scorer import PRIOR_WEIGHTS, RenderContext, composite, loudness_checks, score_render
    from apps.transition_score.signals import click_ratio, mono

_SCORER_NAMES = frozenset({"PRIOR_WEIGHTS", "RenderContext", "composite", "loudness_checks", "score_render"})
_SIGNAL_NAMES = frozenset({"click_ratio", "mono"})


def __getattr__(name: str) -> object:
    if name in _SCORER_NAMES:
        from apps.transition_score import scorer as source
    elif name in _SIGNAL_NAMES:
        from apps.transition_score import signals as source
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(source, name)
    globals()[name] = value
    return value


@dataclass(frozen=True)
class _TryerService:
    autodj: AutoDjService
    source: TrackSource | None = None

    def try_pair(
        self,
        out_id: str,
        in_id: str,
        *,
        n: int = 6,
        context_bars: int = 4,
        data_dir: Path,
        binary: Path | None = None,
        keep_renders_in: Path | None = None,
    ) -> TryResult:
        return _tryer.try_pair(
            out_id,
            in_id,
            n=n,
            context_bars=context_bars,
            data_dir=data_dir,
            source=self.source,
            binary=binary,
            keep_renders_in=keep_renders_in,
            autodj=self.autodj,
        )


def build_service(autodj: AutoDjService, source: TrackSource | None = None) -> TransitionScoreService:
    """The try-er bound to ``autodj`` and, when given, a track source (else the engine's)."""
    return _TryerService(autodj, source)


__all__ = [
    "MAX_COLUMNS",
    "PRIOR_WEIGHTS",
    "PROFILE_POINTS",
    "AudioUnavailable",
    "DeckNotPlannable",
    "EngineProfiles",
    "EngineTrackSource",
    "LaneError",
    "Lanes",
    "LiveAnswer",
    "LiveRequest",
    "PairNotTryable",
    "PlanCache",
    "ProfileReader",
    "RenderContext",
    "TrackSource",
    "TryError",
    "build_lanes",
    "build_service",
    "click_ratio",
    "composite",
    "deck_bar_now",
    "deck_track",
    "incoming_on_render_clock",
    "loudness_checks",
    "mono",
    "outgoing_natural",
    "plan_live",
    "prepare_key",
    "prepare_live",
    "rendered_steps",
    "score_render",
    "shared_start_out_bar",
    "try_pair",
]


def __dir__() -> list[str]:
    return sorted(__all__)
