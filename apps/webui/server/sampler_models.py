"""Edge sampler wire models (request bodies and slot settings), snake_case.

Spec: ``(private path)`` section 3.2 (Slot, Vector).
Wire twin: ``apps/webui/frontend/src/lib/player/sampler/sampler-types.ts``.
Every model forbids extra fields, so a misspelt key is a 422, never silently dropped.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

NAME_MAX, DESCRIPTION_MAX, SET_NAME_MAX = 60, 200, 40

LaunchQuant = Literal["bar", "beat", "4bars", "off"]
KeyMode = Literal["auto", "master_tempo", "off"]
TempoMode = Literal["match", "own"]
PlayMode = Literal["loop", "one_shot", "gate"]
Repeat = Literal["once", "loop", 4, 2, 8]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SlotSettings(_Strict):
    """How a slot launches and plays (the edit modal's settings)."""

    launch: LaunchQuant
    key: KeyMode
    tempo: TempoMode
    mode: PlayMode
    repeat: Repeat
    downbeat_offset: int = Field(ge=-3, le=3)
    level: int = Field(ge=0, le=100)


class EqDb(_Strict):
    """Deck EQ at capture, in dB per band."""

    low: float
    mid: float
    high: float


class StemPart(_Strict):
    """One stem part's mix state at capture."""

    gain_db: float
    mute: bool
    solo: bool


class Stems(_Strict):
    """The four stem parts' mix state at capture."""

    vocals: StemPart
    drums: StemPart
    bass: StemPart
    inst: StemPart


class SamplerVector(_Strict):
    """The deck's mix state at capture, applied at play time (never baked into audio)."""

    trim_db: float
    eq_db: EqDb
    filter: float = Field(ge=-1, le=1)
    stems: Stems | None
    key_shift_st: int
    master_tempo: bool


class SlotPatch(_Strict):
    """A slot edit: name, description and settings, each optional."""

    name: str | None = Field(default=None, max_length=NAME_MAX)
    description: str | None = Field(default=None, max_length=DESCRIPTION_MAX)
    settings: SlotSettings | None = None


class CaptureRequest(_Strict):
    """A deck loop captured into a set at ``index``."""

    stable_id: str
    in_ms: float = Field(ge=0)
    out_ms: float = Field(gt=0)
    beats: int = Field(ge=1)
    vector: SamplerVector
    set_id: str
    index: int = Field(ge=0)


class PinPut(_Strict):
    """Pin or unpin one slot."""

    pinned: bool


class SetPut(_Strict):
    """A named set's name, description and slot layout (None is an empty place)."""

    name: str = Field(max_length=SET_NAME_MAX)
    description: str = Field(max_length=DESCRIPTION_MAX)
    slot_ids: list[str | None] | None


def default_settings(is_loop: bool) -> dict[str, Any]:
    """DEFAULT_SETTINGS in sampler-types.ts; repeat follows defaultRepeat(isLoop)."""
    return SlotSettings(
        launch="bar",
        key="auto",
        tempo="match",
        mode="loop",
        repeat="loop" if is_loop else "once",
        downbeat_offset=0,
        level=80,
    ).model_dump()
