"""LINKAIR-01: ON AIR, decided once, by the engine that owns the deck (FE-logic audit #6646 move 3).

ON AIR on a deck means the ROOM hears it from that laptop (LINK-111): LINK is joined (a solo set
never shows it), that laptop's Speakers are on (unknown is muted), and its mixer lets the deck
through (channel fader x the crossfader law at least ``ON_AIR_GAIN_EPSILON``, Pioneer style).

Before this the page held two rules: its own decks used all three conditions, while the other
laptop's decks used the mixer alone and ignored the Speakers switch the peer already put on the
wire. Now each engine judges its own decks and puts the verdict on its now-playing answer, so
the other laptop renders the owner's verdict and an agent can ask either engine.

The page keeps its instant local tag (a fader move must light it at once, not a poll later); the
shared conformance vectors (``apps/webui/frontend/tests/unit/fixtures/on-air-vectors.json``)
hold that rule and this one to the same answers. The crossfader law stays the page's (it is the
audio graph's), so the page publishes each deck's resulting gain as ``mixer.channels[n].on_air_gain``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Final

#: Linear gain under which a channel counts as closed (-80 dBFS, as link-turn.ts ON_AIR_GAIN_EPSILON).
ON_AIR_GAIN_EPSILON: Final[float] = 1e-4
ON_AIR_REASON: Final[str] = "the room hears it from this laptop: LINK joined, Speakers on, mixer open"


@dataclass(frozen=True)
class OnAirContext:
    """What the engine knows about its own laptop."""

    link_joined: bool
    #: The Speakers switch; None while unknown (no page, an older page), which is muted.
    speakers_on: bool | None

    @staticmethod
    def from_mirror(mirror: dict[str, Any], *, link_joined: bool) -> OnAirContext:
        room = mirror.get("room_output")
        speakers = room.get("speakers_on") if isinstance(room, dict) else None
        return OnAirContext(link_joined=link_joined, speakers_on=speakers if isinstance(speakers, bool) else None)

    @staticmethod
    def demo() -> OnAirContext:
        """The scripted demo partner has no room: its mixer alone decides (its tag says it is not heard)."""
        return OnAirContext(link_joined=True, speakers_on=True)


@dataclass(frozen=True)
class OnAirVerdict:
    #: None when the deck's mixer gain was not published (an older page): never a guess.
    on_air: bool | None
    reason: str


def deck_on_air(*, loaded: bool, gain: float | None, context: OnAirContext) -> OnAirVerdict:
    """The one ON AIR rule for a deck of this engine."""
    if gain is not None and not (math.isfinite(gain) and 0.0 <= gain <= 1.0):
        raise ValueError(f"deck_on_air: gain must be in [0, 1], got {gain}")
    if not loaded:
        return OnAirVerdict(on_air=False, reason="nothing is loaded")
    if not context.link_joined:
        return OnAirVerdict(on_air=False, reason="LINK is not joined, so a solo set never shows ON AIR")
    if context.speakers_on is None:
        return OnAirVerdict(on_air=False, reason="the Speakers switch is not known yet, so it is muted")
    if not context.speakers_on:
        return OnAirVerdict(on_air=False, reason="Speakers are off on this laptop")
    if gain is None:
        return OnAirVerdict(on_air=None, reason="the page did not publish this deck's mixer gain")
    if gain < ON_AIR_GAIN_EPSILON:
        return OnAirVerdict(on_air=False, reason=f"the mixer has it closed (gain {gain:.4f})")
    return OnAirVerdict(on_air=True, reason=ON_AIR_REASON)
