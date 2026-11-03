"""A validated plan turned into the page's ``plan`` order (AGENT-24).

The page executor (``apps/webui/frontend/src/lib/rb/agent-plan.ts``) does no
musical reasoning: it receives bus commands keyed to bars on the CLOCK deck
(the outgoing deck), runs ``prepare`` at once, and fires each event when the
clock deck's playhead crosses its bar, interpolating ramps against the live
playhead. Everything a planner decides is resolved here, in Python, where it
is tested.

Two pieces of DJ craft live in this step rather than in the styles:

* **The incoming deck is silent before it plays.** Every ``set`` on the
  incoming deck at or before bar 0 moves into ``prepare``, so a fader or EQ
  kill is in place before the deck starts, never a beat after.
* **The incoming deck lands on the downbeat.** It is cued to its window's
  first downbeat in ``prepare`` and its ``play`` fires one beat EARLY as a
  quantized launch (LATENCY-02: "scheduled at the next point where this deck's
  beat 1 aligns with the master's beat 1"), so it starts exactly on the plan's
  bar instead of one 16 ms page tick late.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from apps.autodj.plan import (
    PlanEvent,
    TransitionPlan,
    cap_outgoing_fader,
    handover_bar,
    hoistable,
    incoming_leads,
    launch_bar,
    live_child,
    live_outgoing_fader,
    prelaunch_synced_tempo_moves,
    read_live_channels,
    sync_tempo_fold,
    sync_tempo_ratio,
)
from apps.autodj.profile import TrackProfile, trim_knob

_EQ_BANDS = {"eq_low": "low", "eq_mid": "mid", "eq_high": "high"}
#: How far the incoming deck's live tempo (at pitch 1) may sit from its profile's before the mirror counts as stale.
IN_TEMPO_AGREEMENT = 0.005


def control_command(deck: int | None, control: str, value: float) -> dict[str, Any]:
    """One bus command (``performance-ipc.svelte.ts`` union) for a plan control."""
    if control == "crossfader":
        return {"type": "crossfader", "value": value}
    if deck is None:
        raise ValueError(f"{control} needs a deck")
    if control in _EQ_BANDS:
        return {"type": "eq", "deck": deck, "band": _EQ_BANDS[control], "value": value}
    if control == "tempo":
        return {"type": "tempo", "deck": deck, "ratio": value}
    if control in ("fader", "trim", "filter"):
        return {"type": control, "deck": deck, "value": value}
    raise ValueError(f"no bus command for control {control!r}")


def _action_command(event: PlanEvent) -> dict[str, Any]:
    assert event.deck is not None
    if event.name == "play":
        return {"type": "play", "deck": event.deck, "playing": True, "quantize": True}
    if event.name == "stop":
        return {"type": "play", "deck": event.deck, "playing": False}
    if event.name == "master":
        return {"type": "master", "deck": event.deck}
    raise ValueError(f"unknown action {event.name!r}")


def _compile_event(event: PlanEvent, previous_transport: float | None = None) -> dict[str, Any]:
    """One timed executor event: an action, a set, or a ramp."""
    if event.kind == "do":
        at_bar = launch_bar(event.at_bar, previous_transport) if event.name == "play" else event.at_bar
        return {"at_bar": at_bar, "command": _action_command(event)}
    target = event.target()
    command = control_command(event.deck, event.name, target)
    if event.kind == "set":
        return {"at_bar": event.at_bar, "command": command}
    return {"at_bar": event.at_bar, "ramp": {"command": command, "to": target, "over_bars": event.over_bars}}


def _launch_decks(plan: TransitionPlan, live: Mapping[str, Any] | None) -> tuple[Mapping[str, Any], float] | None:
    """The outgoing deck's state and the incoming deck's pitch range, from the mirror; None when either is missing.

    Only a ``sync_to_out`` plan has a launch tempo to match, so any other mode has none.
    """
    if plan.tempo_mode != "sync_to_out" or live is None:
        return None
    decks = live_child(live, "decks", "live mirror decks")
    if not decks:
        return None
    out_state = live_child(decks, str(plan.out.deck), "live mirror deck")
    in_state = live_child(decks, str(plan.in_.deck), "live mirror deck")
    if not isinstance(out_state, Mapping) or not isinstance(in_state, Mapping):
        return None
    range_pct = in_state.get("pitch_range")
    if not isinstance(range_pct, (int, float)):
        return None
    return out_state, float(range_pct)


def _launch_tempo(plan: TransitionPlan, in_profile: TrackProfile, live: Mapping[str, Any] | None) -> float | None:
    """The tempo the paused incoming deck is set to before a ``sync_to_out`` launch.

    A quantized play arms the next downbeat without Beat Sync's tempo match (Beat Sync
    on a paused deck only sets its flag), so the deck is put on the outgoing tempo first,
    with the ratio Beat Sync itself would pick. ``None`` leaves the tempo alone: another
    mode, or a mirror without the tempos and range (validation says so).
    """
    found = _launch_decks(plan, live)
    if found is None:
        return None
    ratio = sync_tempo_ratio(found[0], in_profile, found[1])
    return None if ratio is None else round(ratio, 6)


def _launch_fold(plan: TransitionPlan, in_profile: TrackProfile, live: Mapping[str, Any] | None) -> float | None:
    """The fold (0.5 or 2) the launch tempo folds to, or None when it does not fold or there is no launch tempo.

    The page reports a fold through the shared Beat Sync notice (the BAR contract), so the prepared beat_sync names it.
    """
    found = _launch_decks(plan, live)
    if found is None:
        return None
    return sync_tempo_fold(found[0], in_profile, found[1])


def _beat_sync_command(plan: TransitionPlan, in_profile: TrackProfile, live: Mapping[str, Any] | None) -> dict[str, Any]:
    """The incoming deck's beat_sync prepare step. A folded launch keeps phase but not the bar count, so it names the
    fold and its master for the page (AGENTS.md BAR contract)."""
    sync: dict[str, Any] = {"type": "beat_sync", "deck": plan.in_.deck, "enabled": plan.tempo_mode == "sync_to_out"}
    fold = _launch_fold(plan, in_profile, live)
    if sync["enabled"] and fold is not None and fold != 1.0:
        sync["normalization"] = fold
        sync["master"] = plan.out.deck
    return sync


def _trim_command(plan: TransitionPlan) -> dict[str, Any] | None:
    """AGENT-31: the incoming deck's loudness-match trim, set in ``prepare`` before its fader comes up; None when the
    plan carries no ``trim_db`` (its ``trim_reason`` says why). An authored incoming trim would fight it, so it raises."""
    if plan.in_.trim_db is None:
        return None
    if any(e.deck == plan.in_.deck and e.name == "trim" for e in plan.events):
        raise ValueError("in.trim_db and an authored incoming trim event both set the trim: keep one")
    return control_command(plan.in_.deck, "trim", trim_knob(plan.in_.trim_db))


def _launch_tempo_refreshes(
    plan: TransitionPlan, in_profile: TrackProfile, live: Mapping[str, Any] | None, launch_at: float
) -> list[dict[str, Any]]:
    """Timed incoming-tempo sets that keep a ``sync_to_out`` launch matched to an outgoing tempo change.

    The prepare-time ratio is the outgoing tempo as it stands now. An authored outgoing tempo set that lands
    before the incoming play changes the clock deck's tempo under the paused incoming deck, so the incoming
    tempo is set again at that same bar, to the ratio Beat Sync would pick for the new tempo. The deck stays
    paused until the launch. Empty when the tempo cannot be modeled from the mirror.
    """
    # An authored incoming tempo is the value the plan means, so the refresh must not overwrite it.
    if plan.tempo_mode != "sync_to_out" or _incoming_tempo_moves(plan) or not in_profile.bpm or in_profile.bpm <= 0:
        return []
    inputs = _refresh_inputs(plan, live)
    if inputs is None:
        return []
    grid_bpm, range_pct = inputs
    refreshes: list[dict[str, Any]] = []
    for event in plan.events:
        value = _outgoing_tempo_value(event, plan)
        if value is None or event.at_bar >= launch_at:
            continue
        ratio = sync_tempo_ratio({"effective_bpm": grid_bpm * value}, in_profile, range_pct)
        if ratio is None:
            continue
        refreshes.append({"at_bar": event.at_bar, "command": control_command(plan.in_.deck, "tempo", round(ratio, 6))})
    return refreshes


def _refresh_inputs(plan: TransitionPlan, live: Mapping[str, Any] | None) -> tuple[float, float] | None:
    """The outgoing grid BPM and the incoming pitch range (percent) from the live mirror, or None if either is unknown."""
    decks = live.get("decks") if live is not None else None
    if not isinstance(decks, Mapping):
        return None
    out_state, in_state = decks.get(str(plan.out.deck)), decks.get(str(plan.in_.deck))
    if not isinstance(out_state, Mapping) or not isinstance(in_state, Mapping):
        return None
    range_pct = in_state.get("pitch_range")
    out_bpm, out_pitch = out_state.get("effective_bpm"), out_state.get("pitch")
    if not isinstance(range_pct, (int, float)) or not isinstance(out_bpm, (int, float)) or out_bpm <= 0:
        return None
    if not isinstance(out_pitch, (int, float)) or out_pitch <= 0:
        return None
    # Effective BPM is the grid BPM times the pitch, so the grid BPM is the effective one over the pitch.
    return float(out_bpm) / float(out_pitch), float(range_pct)


def _outgoing_tempo_value(event: PlanEvent, plan: TransitionPlan) -> float | None:
    """The tempo an authored set on the outgoing deck moves to, or None for any other event: the change a launch must follow."""
    if event.kind != "set" or event.deck != plan.out.deck or event.name != "tempo" or event.value is None:
        return None
    return float(event.value)


def _incoming_tempo_moves(plan: TransitionPlan) -> bool:
    """Whether the incoming deck has any authored tempo move, timed or hoisted into prepare."""
    return any(e.deck == plan.in_.deck and e.name == "tempo" for e in plan.events)


def _live_deck(live: Mapping[str, Any] | None, deck: int) -> Mapping[str, Any]:
    decks = live_child((live or {}), "decks", "live mirror decks")
    state = live_child(decks, str(deck), "live mirror deck")
    if not state:
        raise ValueError(f"deck {deck} is not in the live mirror")
    return state


def _positive(state: Mapping[str, Any], key: str, deck: int) -> float:
    value = state.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"deck {deck}'s {key} is unknown in the live mirror")
    return float(value)


@dataclass(frozen=True)
class _LeaderScale:
    """AGENT-32: the live tempos a leader glide is computed from: an incoming ratio ``r`` puts the leader at
    ``in_bpm * r / fold`` over the leader track's own tempo."""

    in_track_bpm: float
    out_track_bpm: float
    out_range: float
    fold: float

    @classmethod
    def of(cls, plan: TransitionPlan, in_profile: TrackProfile, live: Mapping[str, Any] | None) -> _LeaderScale:
        """Needs both live tempos and the leader's pitch range, else it raises: never a guessed ratio."""
        if not in_profile.bpm or in_profile.bpm <= 0:
            raise ValueError(f"{plan.in_.track} has no tempo, so its tempo moves cannot be put on the leader")
        out_state, in_state = _live_deck(live, plan.out.deck), _live_deck(live, plan.in_.deck)
        out_effective = _positive(out_state, "effective_bpm", plan.out.deck)
        out_track_bpm = out_effective / _positive(out_state, "pitch", plan.out.deck)
        out_range = _positive(out_state, "pitch_range", plan.out.deck)
        # The follower's live tempo is required too, and must be its track's: a stale mirror (another track, or no
        # tempo yet) would otherwise produce a leader glide for a deck it does not describe.
        in_track_bpm = _positive(in_state, "effective_bpm", plan.in_.deck) / _positive(in_state, "pitch", plan.in_.deck)
        if abs(in_track_bpm - in_profile.bpm) > in_profile.bpm * IN_TEMPO_AGREEMENT:
            raise ValueError(
                f"deck {plan.in_.deck}'s live tempo ({in_track_bpm:.2f} BPM at pitch 1) is not {plan.in_.track}'s "
                f"{in_profile.bpm:.2f} BPM: the mirror is stale or holds another track"
            )
        ratio = sync_tempo_ratio(out_state, in_profile, _positive(in_state, "pitch_range", plan.in_.deck))
        if ratio is None:
            raise ValueError(
                f"Beat Sync cannot match deck {plan.in_.deck} to the leader, so it cannot follow a leader glide"
            )
        return cls(in_track_bpm, out_track_bpm, out_range, ratio * in_profile.bpm / out_effective)

    def leader_tempo(self, event: PlanEvent, out_deck: int) -> float:
        """The leader ratio for an incoming tempo ``event``; refused outside the leader's pitch range."""
        leader = round(self.in_track_bpm * event.target() / self.fold / self.out_track_bpm, 6)
        if abs(leader - 1.0) * 100.0 > self.out_range + 1e-9:
            raise ValueError(
                f"incoming tempo {event.value:g} at bar {event.at_bar:g} needs leader tempo {leader:g}, outside deck "
                f"{out_deck}'s +-{self.out_range:g}% pitch range"
            )
        return leader


def _refuse_ramp_past_handover(event: PlanEvent, handover: float | None) -> None:
    """A tempo ramp that starts before the handover but runs past it would move the wrong deck for half of it."""
    if handover is not None and event.kind == "ramp" and event.at_bar + event.over_bars > handover:
        raise ValueError(
            f"incoming tempo ramp at bar {event.at_bar:g} runs past the handover at bar {handover:g}: end it by the "
            "handover, or start it there"
        )


def _leader_led_events(
    plan: TransitionPlan, in_profile: TrackProfile, live: Mapping[str, Any] | None
) -> tuple[PlanEvent, ...]:
    """AGENT-32: a synced plan's incoming tempo moves, moved onto the LEADER (the outgoing, master deck).

    Beat Sync rejects tempo changes on a playing follower, and turning it off runs the glide open loop: the decks
    drift apart. Mixxx keeps the follower synced and moves the leader instead (its sync followers track the leader's
    rate), so this does too. An incoming target ratio ``r`` means an incoming tempo of ``in_bpm * r``; the synced
    follower plays ``fold`` of its bars per leader bar, so the leader is set to ``in_bpm * r / fold`` over its own
    track tempo. Needs both live tempos and the leader's pitch range, else it raises: never a guessed ratio.
    """
    if plan.tempo_mode != "sync_to_out" or not _incoming_tempo_moves(plan):
        return plan.events
    if any(e.deck == plan.out.deck and e.name == "tempo" for e in plan.events):
        raise ValueError(
            "a synced plan moves the tempo on one deck only: the incoming moves go to the leader, which has its own"
        )
    early = prelaunch_synced_tempo_moves(plan)
    if early:
        raise ValueError(
            f"incoming tempo at bar {early[0].at_bar:g} runs before the incoming launch in a synced plan: the follower "
            "would start at the old tempo against a moved leader; move it to or after the launch"
        )
    scale = _LeaderScale.of(plan, in_profile, live)
    # From the plan's terminal master event on, the incoming deck IS the leader (the outgoing one may be stopped), so a
    # tempo move there stays on it (Sol P1 #7594). A ramp that starts before the handover but runs past it is refused:
    # half of it would move the wrong deck.
    handover = handover_bar(plan)
    moved: list[PlanEvent] = []
    for index, event in enumerate(plan.events):
        if event.deck != plan.in_.deck or event.name != "tempo":
            moved.append(event)
            continue
        if incoming_leads(plan, index):
            moved.append(event)
            continue
        _refuse_ramp_past_handover(event, handover)
        moved.append(replace(event, deck=plan.out.deck, value=scale.leader_tempo(event, plan.out.deck)))
    return tuple(moved)


def _refreshes_before_launch(
    plan: TransitionPlan, in_profile: TrackProfile, live: Mapping[str, Any] | None, events: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The launch-tempo refreshes, timed against the first play of the incoming deck among the compiled events."""
    play_bars = [
        row["at_bar"]
        for row in events
        if row.get("command", {}).get("deck") == plan.in_.deck and row.get("command", {}).get("type") == "play"
    ]
    return _launch_tempo_refreshes(plan, in_profile, live, min(play_bars)) if play_bars else []


def compile_plan(
    plan: TransitionPlan,
    in_profile: TrackProfile,
    *,
    plan_id: str,
    live: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The ``{"plan": {...}}`` order the page executes."""
    prepare: list[dict[str, Any]] = []
    if live is not None and live.get("master_deck") != plan.out.deck:
        # A quantized launch aligns to the MASTER's downbeat, and the plan's
        # bars are counted on the outgoing deck, so they must be the same deck.
        prepare.append({"type": "master", "deck": plan.out.deck})
    cue_ms = round(in_profile.time_of_bar(plan.in_.start_bar) * 1000.0, 3)
    # exact: grid quantize would re-snap the cue to another phrase, so the window must land as authored.
    prepare.append({"type": "seek", "deck": plan.in_.deck, "position_ms": cue_ms, "exact": True})
    trim = _trim_command(plan)
    if trim is not None:
        prepare.append(trim)
    ratio = _launch_tempo(plan, in_profile, live)
    if ratio is not None:
        prepare.append(control_command(plan.in_.deck, "tempo", ratio))
    # Explicit both ways: a fresh deck defaults Beat Sync on, and an idle deck
    # keeps it from its last track, so "none" must turn it off. A synced plan keeps it on through a glide: the
    # incoming tempo moves run on the leader instead (AGENT-32, ``_leader_led_events``).
    prepare.append(_beat_sync_command(plan, in_profile, live))
    led = replace(plan, events=_leader_led_events(plan, in_profile, live))
    # AGENT-35: compile reads the live channels exactly as validation does, so a malformed fader, trim or low EQ on
    # either deck refuses the order (Sol P1 #7595), then caps the outgoing fader; without a live fader, say so.
    read_live_channels(plan, live)
    ceiling = live_outgoing_fader(plan, live)
    notes: list[str] = []
    if ceiling is None:
        notes.append("no live outgoing fader in the mirror: outgoing fader levels are used as planned, uncapped")
    # Sol P1 #7595: the levels are checked on every path; with no live fader the ceiling is unity, which caps nothing.
    led = cap_outgoing_fader(led, 1.0 if ceiling is None else ceiling)
    events: list[dict[str, Any]] = []
    last_transport: dict[int | None, float] = {}
    for event in led.events:
        if hoistable(event, led):
            prepare.append(control_command(event.deck, event.name, event.target()))
            continue
        events.append(_compile_event(event, last_transport.get(event.deck)))
        if event.kind == "do" and event.name in ("play", "stop"):
            last_transport[event.deck] = event.at_bar
    events.extend(_refreshes_before_launch(plan, in_profile, live, events))
    events.sort(key=lambda row: row["at_bar"])
    return {
        "plan": {
            "id": plan_id,
            "clock": {"deck": plan.out.deck, "stable_id": plan.out.track},
            "incoming": {"deck": plan.in_.deck, "stable_id": plan.in_.track},
            "start_bar": plan.out.start_bar,
            "bars": plan.bars,
            "style": plan.style,
            "prepare": prepare,
            "events": events,
            "notes": notes,
        }
    }
