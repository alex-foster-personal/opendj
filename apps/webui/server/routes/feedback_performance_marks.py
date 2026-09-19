"""Durable, agent-readable performance-feedback marks.

The browser records a detached playback snapshot at the IPC dispatch boundary.
This router owns the durable copy under ``<data-dir>/feedback`` so a desktop
restart on a different local port does not discard user judgements.
"""

from __future__ import annotations

import threading
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from .feedback import _dir, _load, _save

router = APIRouter(prefix="/feedback", tags=["feedback"])

_MARKS_FILE = "performance-marks.json"
_MARKS_LOCK = threading.Lock()
_HISTORY_CAP = 400


class LoopMarkOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    in_ms: float
    out_ms: float


class DeckMarkOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    deck_id: int
    stable_id: str | None
    playing: bool
    audible: bool
    position_ms: float
    loop: LoopMarkOut | None


class MixerChannelMarkOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    deck_id: int
    trim: float
    eq_low: float
    eq_mid: float
    eq_high: float
    filter: float = 0.5  # neutral dead-zone default: marks from before issue #990 lack this field
    fader: float
    assign: str


class MixerMarkOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    crossfader: float
    master: float
    channels: list[MixerChannelMarkOut]


class PerformanceFeedbackMarkIn(BaseModel):
    model_config = ConfigDict(frozen=True)

    recorded_at_ms: int = Field(ge=0)
    vote: Literal["bad", "good", "great"]
    decks: list[DeckMarkOut]
    mixer: MixerMarkOut


class PerformanceFeedbackMarksOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    count: int = Field(ge=0, le=_HISTORY_CAP)
    last_mark: PerformanceFeedbackMarkIn | None


def _summary(items: list[dict]) -> PerformanceFeedbackMarksOut:
    marks = [PerformanceFeedbackMarkIn.model_validate(item) for item in items]
    return PerformanceFeedbackMarksOut(
        count=len(marks), last_mark=marks[-1] if marks else None
    )


@router.get("/performance-marks", response_model=PerformanceFeedbackMarksOut)
def get_performance_marks(request: Request) -> PerformanceFeedbackMarksOut:
    path = _dir(request) / _MARKS_FILE
    with _MARKS_LOCK:
        return _summary(_load(path, "marks"))


@router.post(
    "/performance-marks",
    response_model=PerformanceFeedbackMarksOut,
    status_code=201,
)
def create_performance_mark(
    body: PerformanceFeedbackMarkIn, request: Request
) -> PerformanceFeedbackMarksOut:
    path = _dir(request) / _MARKS_FILE
    with _MARKS_LOCK:
        marks = _load(path, "marks")
        # Validate existing disk data before append. A corrupt history must not
        # be silently truncated by a fresh user action.
        _summary(marks)
        marks.append(body.model_dump())
        kept = marks[-_HISTORY_CAP:]
        _save(path, "marks", kept)
        return _summary(kept)
