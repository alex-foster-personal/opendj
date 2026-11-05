"""Reading a plan against the live mirror: the shared half of ``opendj dj`` and the app's own planner call.

Both anchor a plan on the outgoing deck's live bar, and both hold it to the same lead. They live here, below the CLI
and the web UI, because neither of those may import the other (import-linter ``webui-is-the-top-layer``).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from apps.autodj.plan import TransitionPlan, first_dispatch_bar
from apps.autodj.profile import TrackProfile

#: Bars of lead a live anchor leaves before the plan's first event, so the page has time to cue the incoming deck.
ANCHOR_LEAD_BARS = 1.0


def live_bar(profile: TrackProfile, deck_state: Mapping[str, Any]) -> float | None:
    """The deck's playhead on the profile's bar clock; None when the deck holds another track or reports no playhead."""
    if deck_state.get("stable_id") != profile.stable_id:
        return None
    position = deck_state.get("position")
    position_ms = position.get("ms") if isinstance(position, Mapping) else None
    if not isinstance(position_ms, (int, float)):
        return None
    return profile.bar_at(float(position_ms) / 1000.0)


def lead_short(plan: TransitionPlan, bar_now: float | None) -> str | None:
    """Sol #7595: the reason a plan's first command is no longer ANCHOR_LEAD_BARS ahead of the playhead, else None.

    The search required that lead; the final reread must hold every candidate to it too, not only to
    validate_plan's smaller position margin. No playhead means nothing to measure against."""
    if bar_now is None:
        return None
    first = plan.out.start_bar + first_dispatch_bar(plan)
    if first >= bar_now + ANCHOR_LEAD_BARS:
        return None
    return f"anchor_lead: first command at bar {first:.2f} is under {ANCHOR_LEAD_BARS:g} bar ahead of the playhead at {bar_now:.2f}"


def with_in_track(mirror: Mapping[str, Any], in_deck: int, in_id: str, in_bpm: float | None) -> dict[str, Any]:
    """The mirror as it will be once ``schedule`` has loaded ``in_id``.

    ``schedule`` loads an idle incoming deck itself, so validating a proposal
    against the deck's CURRENT track would refuse a plan that will be fine.
    The tempos are the incoming track's own, not the previous track's.
    """
    copy = dict(mirror)
    decks = {
        key: dict(value) if isinstance(value, Mapping) else value for key, value in (mirror.get("decks") or {}).items()
    }
    state = decks.get(str(in_deck))
    if isinstance(state, dict) and state.get("stable_id") != in_id and state.get("playing") is not True:
        state["stable_id"] = in_id
        state["bpm"] = in_bpm
        state["effective_bpm"] = in_bpm
    copy["decks"] = decks
    return copy


def heard_on_deck(profile: TrackProfile, deck_state: Mapping[str, Any], *, with_tempo: bool) -> TrackProfile:
    """AUTOPLAY-HEARD: ``profile`` as the deck plays it: its key shifted by the deck's applied key shift, and (for the
    master, ``with_tempo``) its tempo at the deck's pitch. The profile unchanged when the deck holds another track."""
    if deck_state.get("stable_id") != profile.stable_id:
        return profile
    key_sync = deck_state.get("key_sync")
    shift = key_sync.get("shift_semitones") if isinstance(key_sync, Mapping) else None
    effective = deck_state.get("effective_bpm") if with_tempo else None
    pitch_range = deck_state.get("pitch_range")
    return profile.as_heard(
        pitch_range_pct=float(pitch_range) if isinstance(pitch_range, (int, float)) and pitch_range > 0 else None,
        key_shift=shift if isinstance(shift, int) and not isinstance(shift, bool) else 0,
        heard_bpm=float(effective) if isinstance(effective, (int, float)) and effective > 0 else None,
    )
