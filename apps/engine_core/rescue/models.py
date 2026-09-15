"""Pydantic models for the performance rescue ring wire contract."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

DeckOutcomeKind = Literal["resumed", "paused", "missing"]
RestoreMode = Literal["layout", "play"]


class RescueSnapshotMeta(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    captured_at_ms: int
    age_ms: int
    deck_count_loaded: int = Field(description="Decks with a non-empty stable_id at capture")
    playlist_id: str | None = None


class RescueSnapshotsOut(BaseModel):
    snapshots: list[RescueSnapshotMeta]


class RescueDeckOutcomeOut(BaseModel):
    outcome: DeckOutcomeKind
    stable_id: str | None = None


class RescueRestoreIn(BaseModel):
    play: bool = False
    snapshot_id: str | None = None


class RescueRestoreOut(BaseModel):
    snapshot_id: str
    captured_at_ms: int
    mode: RestoreMode
    payload: dict
    decks: dict[str, RescueDeckOutcomeOut]
