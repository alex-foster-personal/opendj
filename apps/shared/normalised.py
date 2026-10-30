"""Normalised cross-app shapes for Phase 4 sync (RB + djay).

These dataclasses are the lingua franca between Rekordbox and djay Pro readers
and writers. They mirror the open-dj C5 strawman intent so Phase 15/16
adapters won't need to re-map.

See ``(private path)`` §9.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

#: ``transition_in`` (AGENT-51) is where the user expects an incoming transition
#: to land: Traktor's Fade In cue, Mixxx's intro start. rekordbox has no such type.
CueKind = Literal["memory", "hot", "load", "loop", "transition_in"]


@dataclass(slots=True, frozen=True)
class NormalisedCue:
    """One cue point, normalised across RB and djay.

    Attributes
    ----------
    position_msec
        Milliseconds from track start.
    kind
        ``"memory"`` / ``"hot"`` / ``"load"`` / ``"loop"`` / ``"transition_in"``.
    index
        Hot-cue slot 0..7 for ``kind="hot"``, else ``None``.
    color_rgb
        ``(r, g, b)`` triple or ``None`` if no colour set.
    name
        Cue comment / label, or ``None``.
    loop_length_msec
        Only set when ``kind == "loop"``.
    """

    position_msec: int
    kind: CueKind
    index: int | None = None
    color_rgb: tuple[int, int, int] | None = None
    name: str | None = None
    loop_length_msec: int | None = None


@dataclass(slots=True, frozen=True)
class NormalisedAnalysis:
    """Per-track analysis metadata, normalised across RB and djay."""

    uuid_or_id: str
    source: Literal["rb", "djay"]
    bpm: float | None = None
    manual_bpm: float | None = None
    key_camelot: str | None = None
    # The user's color label (RB ``ColorID`` / djay ``colorIndex``). Neither
    # library analyzes energy, so there is deliberately no energy field here.
    color_index: int | None = None
    tags: str | None = None
    is_straight_grid: bool | None = None
    # Per-side last-modified timestamp used by the newest-wins conflict
    # resolver. Codex P04-01: without this, ``--prefer newest`` in
    # ``apps.audit.sync_diff`` silently collapsed to RB-default for every
    # row because timestamps were never plumbed through.
    modified_at: datetime | None = None


__all__ = ["CueKind", "NormalisedCue", "NormalisedAnalysis"]
