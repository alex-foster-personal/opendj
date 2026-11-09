"""Show runtime: plays each deck's show plan and blends decks by weight (LIGHT-62 in the lighting spec).

Pure, no I/O. Each playing deck shows the plan cue at its beat; layer intensity is the deck weight.
Colour and position come from one owner deck, which changes only on the heavier deck's downbeat and
is held at least one bar so colour never flips within 4 beats. In ``master_only`` the followed deck
hands over at the incoming track's next phrase boundary. No playing deck means ambient (L55).

The input types are local stand-ins for L61 (show plan) and L04 (deck weights); the output layers
feed L53's compositor.
"""

from __future__ import annotations

import bisect
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from apps.lighting.beat_clock import BEATS_PER_BAR

MIN_OWNER_HOLD_BEATS = 4
MAX_EFFECT_LEVEL = 2


class HandoverMode(StrEnum):
    BLEND = "blend"
    UPFADER_ONLY = "upfader_only"
    MASTER_ONLY = "master_only"


@dataclass(frozen=True)
class ShowCue:
    at_beat: int
    look: str
    effect_level: int
    hit: bool = False

    def __post_init__(self) -> None:
        if not 0 <= self.effect_level <= MAX_EFFECT_LEVEL:
            raise ValueError(
                f"cue at beat {self.at_beat}: effect_level {self.effect_level} outside 0..{MAX_EFFECT_LEVEL}"
            )


@dataclass(frozen=True)
class DeckPlan:
    track_id: str
    cues: tuple[ShowCue, ...]
    phrase_beats: tuple[int, ...]

    def __post_init__(self) -> None:
        if not self.cues:
            raise ValueError(f"plan {self.track_id}: no cues")
        starts = [c.at_beat for c in self.cues]
        if starts != sorted(starts) or list(self.phrase_beats) != sorted(self.phrase_beats):
            raise ValueError(f"plan {self.track_id}: cues and phrase_beats must be sorted")


@dataclass(frozen=True)
class DeckState:
    deck: str
    playing: bool
    beat_index: int
    bar_beat: int
    weight: float
    plan: DeckPlan | None


@dataclass(frozen=True)
class DeckLayer:
    deck: str
    look: str
    effect_level: int
    intensity: float
    hit: bool


@dataclass(frozen=True)
class ShowFrame:
    ambient: bool
    layers: tuple[DeckLayer, ...]
    colour_deck: str | None
    position_deck: str | None


@dataclass(frozen=True)
class ShowState:
    owner: str | None = None
    owner_since_beat: int | None = None
    last_beat: Mapping[str, int] = field(default_factory=dict)


AMBIENT_FRAME = ShowFrame(ambient=True, layers=(), colour_deck=None, position_deck=None)


# ----------------------------------------------------------------------------- helpers


def _validate(decks: Sequence[DeckState]) -> None:
    seen: set[str] = set()
    for d in decks:
        if d.deck in seen:
            raise ValueError(f"deck {d.deck}: duplicate deck id")
        seen.add(d.deck)
        if not 0.0 <= d.weight <= 1.0:
            raise ValueError(f"deck {d.deck}: weight {d.weight} outside 0..1")
        if d.playing and d.plan is None:
            raise ValueError(f"deck {d.deck}: playing with no show plan")
        if not 1 <= d.bar_beat <= BEATS_PER_BAR:
            raise ValueError(f"deck {d.deck}: bar_beat {d.bar_beat} outside 1..{BEATS_PER_BAR}")


def _crossed_downbeat(deck: DeckState, state: ShowState) -> bool:
    return deck.bar_beat == 1 and deck.beat_index != state.last_beat.get(deck.deck)


def _crossed_phrase(deck: DeckState, state: ShowState) -> bool:
    assert deck.plan is not None
    last = state.last_beat.get(deck.deck, deck.beat_index - 1)
    return any(last < b <= deck.beat_index for b in deck.plan.phrase_beats)


def _heaviest(playing: Sequence[DeckState], owner: str | None) -> DeckState:
    top = max(d.weight for d in playing)
    tied = [d for d in playing if d.weight == top]
    return next((d for d in tied if d.deck == owner), tied[0])


def _layer(deck: DeckState, intensity: float) -> DeckLayer:
    assert deck.plan is not None
    cue = current_cue(deck.plan, deck.beat_index)
    return DeckLayer(
        deck=deck.deck,
        look=cue.look,
        effect_level=cue.effect_level,
        intensity=intensity,
        hit=cue.hit and deck.beat_index == cue.at_beat,
    )


def _next_owner(state: ShowState, playing: Sequence[DeckState], mode: HandoverMode) -> DeckState:
    by_id = {d.deck: d for d in playing}
    current = by_id.get(state.owner) if state.owner is not None else None
    candidate = _heaviest(playing, state.owner)
    if current is None:
        return candidate
    if candidate.deck == current.deck:
        return current
    if mode in (HandoverMode.BLEND, HandoverMode.UPFADER_ONLY):
        held = current.beat_index - (state.owner_since_beat or 0)
        switch = (
            candidate.weight > current.weight and _crossed_downbeat(candidate, state) and held >= MIN_OWNER_HOLD_BEATS
        )
    elif mode == HandoverMode.MASTER_ONLY:
        switch = _crossed_phrase(candidate, state)
    else:
        raise ValueError(f"unknown handover mode {mode}")
    return candidate if switch else current


# ----------------------------------------------------------------------------- public


def current_cue(plan: DeckPlan, beat_index: int) -> ShowCue:
    """The last cue starting at or before ``beat_index``; the first cue before the plan starts."""
    i = bisect.bisect_right([c.at_beat for c in plan.cues], beat_index) - 1
    return plan.cues[max(i, 0)]


def step(state: ShowState, decks: Sequence[DeckState], mode: HandoverMode) -> tuple[ShowState, ShowFrame]:
    """Advance one frame: pick the colour owner, build one layer per contributing deck."""
    _validate(decks)
    playing = [d for d in decks if d.playing]
    if not playing:
        return ShowState(), AMBIENT_FRAME
    owner = _next_owner(state, playing, mode)
    since = state.owner_since_beat if owner.deck == state.owner else owner.beat_index
    if mode in (HandoverMode.BLEND, HandoverMode.UPFADER_ONLY):
        layers = tuple(_layer(d, d.weight) for d in sorted(playing, key=lambda d: d.deck) if d.weight > 0)
    elif mode == HandoverMode.MASTER_ONLY:
        layers = (_layer(owner, 1.0),)
    else:
        raise ValueError(f"unknown handover mode {mode}")
    new_state = ShowState(owner=owner.deck, owner_since_beat=since, last_beat={d.deck: d.beat_index for d in playing})
    return new_state, ShowFrame(ambient=False, layers=layers, colour_deck=owner.deck, position_deck=owner.deck)
