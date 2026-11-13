"""Compose lighting clock policy with the current performance-page mirror."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, StrictStr, model_validator
from pydantic.json_schema import SkipJsonSchema

from apps.lighting.contract import (
    ClockSnapshot,
    ClockSourceError,
    ClockState,
    normalize_clock_deck,
    parse_clock_source,
)
from apps.shared.telemetry import MIRROR_LIVE_MAX_AGE_S, mirror_age_s


def clock_snapshot(app: FastAPI, source: str) -> ClockSnapshot:
    mirror: Any = getattr(app.state, "ui_mirror", None)
    if not isinstance(mirror, dict):
        return ClockSnapshot(clock_source=source, unavailable_reason="lighting_clock_mirror_unavailable")
    age = mirror_age_s(mirror)
    if age is None or age > MIRROR_LIVE_MAX_AGE_S:
        return ClockSnapshot(clock_source=source, unavailable_reason="lighting_clock_mirror_stale")
    decks = mirror.get("decks")
    loaded = frozenset(
        normalized
        for deck, state in (decks.items() if isinstance(decks, dict) else ())
        if (normalized := normalize_clock_deck(deck)) is not None
        and isinstance(state, dict)
        and isinstance(state.get("stable_id"), str)
        and state["stable_id"].strip()
    )
    return ClockSnapshot(
        clock_source=source,
        master_deck=normalize_clock_deck(mirror.get("master_deck")),
        loaded_decks=loaded,
    )


class LightingClockController:
    """Persistence stays in the prefs writer; every resolution uses one current mirror."""

    def __init__(
        self,
        app: FastAPI,
        read_source: Callable[[], str],
        persist_source: Callable[[str], None],
    ) -> None:
        self._app = app
        self._read_source = read_source
        self._persist_source = persist_source

    def snapshot(self) -> ClockSnapshot:
        return clock_snapshot(self._app, self._read_source())

    def read(self) -> ClockState:
        return self.snapshot().resolve()

    def set_source(self, source: str) -> ClockState:
        self._persist_source(source)
        return self.read()


class LightingPrefsOut(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    clock_source: StrictStr = "master"


class LightingPrefsPatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    clock_source: StrictStr | SkipJsonSchema[None] = None

    @model_validator(mode="after")
    def refuse_null_source(self) -> LightingPrefsPatch:
        if "clock_source" in self.model_fields_set and self.clock_source is None:
            raise ValueError("clock_source must not be null")
        return self


def parse_lighting_prefs(raw: dict[str, Any]) -> dict[str, str]:
    if "lighting" not in raw:
        return {"clock_source": "master"}
    lighting = raw["lighting"]
    if not isinstance(lighting, dict):
        raise HTTPException(422, detail={"code": "UI_PREFS_INVALID", "message": "lighting must be an object"})
    source = lighting.get("clock_source", "master")
    try:
        source = parse_clock_source(source)
    except ClockSourceError as exc:
        raise HTTPException(422, detail={"code": "UI_PREFS_INVALID", "message": str(exc)}) from exc
    return {"clock_source": source}
