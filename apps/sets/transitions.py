"""Structural transition detection over a set timeline.

Plan 12-02 Step 1. Reads events via :class:`apps.sets.state.SetsState`
and returns one :class:`Transition` per ``track_change`` boundary.
Each transition carries hand-computable features the classifier
(:mod:`apps.sets.classify`) consumes.

Features (no knob telemetry in v1; D4 in CONTEXT):

* ``overlap_s``         -- seconds both decks were loaded simultaneously
* ``fade_s``            -- duration from track_change to the last event
                            on the outgoing deck (proxy for fader fall)
* ``incoming_preload_s`` -- seconds the incoming track was loaded before
                            the change (negative if loaded after change)
* ``outgoing_trail_s``  -- time from track_change to the last outgoing
                            event (== fade_s in v1 but kept separate
                            to grow when knob telemetry arrives)
* ``time_since_prev_transition_s`` -- seconds from previous change
* ``is_same_deck_reload`` -- 1.0 if the incoming track reloads on the
                             outgoing deck (quick-double signal), else 0
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .state import Event, SetsState


@dataclass
class Transition:
    """One detected transition boundary.

    ``idx`` is the stable 0-based index in the session's transition
    stream; ``features`` are computed in :func:`compute_features`.
    """

    idx: int
    t_start_s: float
    t_change_s: float
    t_end_s: float
    from_deck: str | None
    to_deck: str | None
    from_track: str | None
    to_track: str | None
    features: dict[str, float] = field(default_factory=dict)


def _last_loaded_on_deck(events: list[Event], deck: str | None, before_idx: int) -> Event | None:
    """Return the last ``track_loaded`` on ``deck`` strictly before ``before_idx``."""
    if deck is None:
        return None
    for e in reversed(events[:before_idx]):
        if e.action == "track_loaded" and e.deck == deck:
            return e
    return None


def _first_loaded_on_deck(events: list[Event], deck: str | None, from_idx: int) -> Event | None:
    if deck is None:
        return None
    for e in events[from_idx:]:
        if e.action == "track_loaded" and e.deck == deck:
            return e
    return None


def _last_event_on_deck(events: list[Event], deck: str | None, after_idx: int) -> Event | None:
    """Last event on ``deck`` after ``after_idx`` but before the next ``track_change``.

    Bounding by the next track_change keeps overlap windows scoped to the
    current transition; otherwise a later track_loaded on the same deck
    balloons the fade.
    """
    if deck is None:
        return None
    last: Event | None = None
    for e in events[after_idx + 1 :]:
        if e.action == "track_change":
            break
        if e.action == "track_loaded" and e.deck == deck:
            # A new load on the outgoing deck means the old outgoing
            # audio is already done; stop scanning so fade_s stays bounded.
            break
        if e.deck == deck:
            last = e
    return last


def find_transitions(
    session_id: str,
    *,
    state: SetsState | None = None,
    events: list[Event] | None = None,
) -> list[Transition]:
    """Return :class:`Transition` objects for a session.

    Either ``state`` OR ``events`` must be provided. ``events`` is the
    pure functional hook used by tests (no DB needed).
    """
    if events is None:
        if state is None:
            raise ValueError("pass state=... or events=...")
        events = state.fetch_events(session_id)

    transitions: list[Transition] = []
    prev_change_ts: float | None = None
    for idx_global, ev in enumerate(events):
        if ev.action != "track_change":
            continue
        from_deck = ev.value.get("from_deck") or None
        to_deck = ev.value.get("to_deck") or ev.deck
        from_uuid = ev.value.get("from_uuid") or None
        to_uuid = ev.value.get("to_uuid") or ev.track_stable_id

        outgoing_load = _last_loaded_on_deck(events, from_deck, idx_global)
        incoming_load = _last_loaded_on_deck(events, to_deck, idx_global)
        if incoming_load is None:
            incoming_load = _first_loaded_on_deck(events, to_deck, idx_global)
        outgoing_last = _last_event_on_deck(events, from_deck, idx_global)

        t_start = (
            incoming_load.timestamp_s
            if incoming_load is not None
            else ev.timestamp_s
        )
        t_change = ev.timestamp_s
        t_end = outgoing_last.timestamp_s if outgoing_last is not None else t_change

        transition = Transition(
            idx=len(transitions),
            t_start_s=t_start,
            t_change_s=t_change,
            t_end_s=t_end,
            from_deck=from_deck,
            to_deck=to_deck,
            from_track=from_uuid,
            to_track=to_uuid,
        )
        transition.features = compute_features(
            transition,
            outgoing_load=outgoing_load,
            incoming_load=incoming_load,
            outgoing_last=outgoing_last,
            prev_change_ts=prev_change_ts,
        )
        transitions.append(transition)
        prev_change_ts = t_change
    return transitions


def compute_features(
    transition: Transition,
    *,
    outgoing_load: Event | None,  # noqa: ARG001 - kept for its keyword callers
    incoming_load: Event | None,
    outgoing_last: Event | None,
    prev_change_ts: float | None,
) -> dict[str, float]:
    """Return a feature dict for ``transition``.

    Pure function: no state + no IO. The classifier depends on this
    signature; do not rename keys without also updating the classifier's
    ``FEATURE_COLUMNS`` list.
    """
    t_change = transition.t_change_s
    incoming_t = incoming_load.timestamp_s if incoming_load is not None else t_change
    outgoing_end = outgoing_last.timestamp_s if outgoing_last is not None else t_change

    overlap_s = max(0.0, outgoing_end - incoming_t)
    fade_s = max(0.0, outgoing_end - t_change)
    incoming_preload_s = t_change - incoming_t
    outgoing_trail_s = fade_s
    time_since_prev = (
        t_change - prev_change_ts if prev_change_ts is not None else 0.0
    )
    is_same_deck_reload = (
        1.0 if (transition.from_deck == transition.to_deck and transition.from_deck is not None) else 0.0
    )

    return {
        "overlap_s": round(overlap_s, 6),
        "fade_s": round(fade_s, 6),
        "incoming_preload_s": round(incoming_preload_s, 6),
        "outgoing_trail_s": round(outgoing_trail_s, 6),
        "time_since_prev_transition_s": round(time_since_prev, 6),
        "is_same_deck_reload": is_same_deck_reload,
    }


FEATURE_COLUMNS: tuple[str, ...] = (
    "overlap_s",
    "fade_s",
    "incoming_preload_s",
    "outgoing_trail_s",
    "time_since_prev_transition_s",
    "is_same_deck_reload",
)


__all__ = [
    "Transition",
    "find_transitions",
    "compute_features",
    "FEATURE_COLUMNS",
]
