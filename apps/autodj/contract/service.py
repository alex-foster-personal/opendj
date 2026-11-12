"""The service interface ``apps.autodj`` offers its callers (ADR-NEW-module-contracts).

Domain consumers receive an :class:`AutoDjService` from ``apps.autodj.http``
and never build the implementation themselves.
"""

from __future__ import annotations

import collections.abc as _collections_abc
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import JsonValue

from .models import (
    ChooserCandidate,
    PlanEvent,
    RenderLayout,
    StyleSummary,
    TrackProfile,
    TransitionPlan,
    ValidationResult,
)


@runtime_checkable
class AutoDjService(Protocol):
    def list_styles(self) -> list[StyleSummary]: ...

    def choose_for_set(
        self, candidates: list[ChooserCandidate], intent: str | None, recent_styles: _collections_abc.Sequence[str]
    ) -> ChooserCandidate | None: ...

    def compile_plan(
        self,
        plan: TransitionPlan,
        in_profile: TrackProfile,
        *,
        plan_id: str,
        live: _collections_abc.Mapping[str, JsonValue] | None = None,
    ) -> dict[str, JsonValue]: ...

    def heard_on_deck(
        self, profile: TrackProfile, deck_state: _collections_abc.Mapping[str, JsonValue], *, with_tempo: bool
    ) -> TrackProfile: ...

    def lead_short(self, plan: TransitionPlan, bar_now: float | None) -> str | None: ...

    def live_bar(
        self, profile: TrackProfile, deck_state: _collections_abc.Mapping[str, JsonValue]
    ) -> float | None: ...

    def with_in_track(
        self,
        mirror: _collections_abc.Mapping[str, JsonValue],
        in_deck: int,
        in_id: str,
        in_bpm: float | None,
    ) -> dict[str, JsonValue]: ...

    def on_decks(
        self, plan: _collections_abc.Mapping[str, JsonValue], out_deck: int, in_deck: int
    ) -> dict[str, JsonValue]: ...

    def live_mapping(
        self, value: JsonValue, where: str = "live mirror value"
    ) -> _collections_abc.Mapping[str, JsonValue]: ...

    def parse_plan(self, document: JsonValue) -> TransitionPlan: ...

    def validate_plan(
        self,
        plan: TransitionPlan,
        out_profile: TrackProfile,
        in_profile: TrackProfile,
        *,
        live: _collections_abc.Mapping[str, JsonValue] | None = None,
    ) -> ValidationResult: ...

    def match_live_trim_db(
        self,
        out_profile: TrackProfile,
        in_profile: TrackProfile,
        live: _collections_abc.Mapping[str, JsonValue] | None,
        out_deck: int,
        in_deck: int,
    ) -> tuple[float | None, str]: ...

    def parse_intent(self, text: str | None) -> str: ...

    def propose_transition(  # noqa: PLR0913 - mirrors the planner's keyword-only public operation
        self,
        out_profile: TrackProfile,
        in_profile: TrackProfile,
        *,
        out_deck: int,
        in_deck: int,
        intent: str | None = None,
        min_out_bar: float | None = None,
        live: dict[str, JsonValue] | None = None,
        top: int | None = 3,
        around_bar: float | None = None,
    ) -> dict[str, JsonValue]: ...

    def profile_from_payloads(
        self,
        anlz: _collections_abc.Mapping[str, JsonValue],
        track: _collections_abc.Mapping[str, JsonValue] | None = None,
    ) -> TrackProfile: ...

    def compile_render_plan(
        self,
        plan: TransitionPlan,
        out_profile: TrackProfile,
        in_profile: TrackProfile,
        context_bars: int = 4,
        *,
        out_audio: Path,
        in_audio: Path,
        start_out_bar: float | None = None,
    ) -> dict[str, JsonValue]: ...

    def layout_of(
        self,
        plan: TransitionPlan,
        out_profile: TrackProfile,
        in_profile: TrackProfile,
        context_bars: int = 4,
        *,
        start_out_bar: float | None = None,
    ) -> RenderLayout: ...

    def effective_bars(self, plan: TransitionPlan) -> tuple[float, ...]: ...

    def renders_at_start(self, event: PlanEvent, plan: TransitionPlan, layout: RenderLayout) -> bool: ...

    def surviving_transport(self, events: _collections_abc.Sequence[PlanEvent], deck: int) -> list[PlanEvent]: ...
