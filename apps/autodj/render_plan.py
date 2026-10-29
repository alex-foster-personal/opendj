"""A TransitionPlan compiled for the offline renderer, ``odj-audio render --plan`` (TRYER-02).

:mod:`apps.autodj.compile` turns a plan into the PAGE's order; this module
turns the same plan into the JSON ``apps/audio-engine/src/plan.rs`` accepts, so
the try-er can hear a candidate before anyone plays it. Differences from the
page compile, each deliberate:

* **Tempo is explicit.** ``plan.rs`` refuses ``beat_sync`` (an offline render
  has no live leader to follow), so ``sync_to_out`` is resolved here to the
  ratio Beat Sync would pick (raw, half or double time, first inside the
  6 % reach) and set on the incoming deck before it plays. When no fold fits,
  the raw ratio is set anyway on a wide pitch range: the render then plays
  the stretch the plan really asks for, and the scorer's stretch veto
  (> 8 %) says so, rather than this step quietly picking something else.
* **The incoming deck plays on the frame.** The engine fires a deck-relative
  event on the first frame the clock deck is at or past its bar, so ``play``
  is not sent one beat early as a quantized launch the way the page sends it.
* **Context.** The render starts ``context_bars`` before the plan's first bar
  (on the outgoing deck) and ends ``context_bars`` after its last (on the
  incoming deck), so the scorer hears each deck alone on either side.
* ``master`` actions are dropped: they only choose Beat Sync's leader, which
  the explicit tempo above already settled.

Everything is a pure function of the plan, the two profiles and the two audio
paths, so the same inputs always give byte-identical JSON (and so the same
``plan_sha256`` from the renderer).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apps.autodj.plan import (
    BEATMATCH_LIMIT_PCT,
    PlanEvent,
    TransitionPlan,
    hoistable,
    pair_beat_sync_fold,
    play_start_bar,
)
from apps.autodj.profile import TrackProfile, trim_knob

#: The renderer's rate; 48 kHz matches the engine's default and the page.
RENDER_SAMPLE_RATE = 48000
#: Pitch ranges the engine accepts (deck.rs ``set_pitch_range``).
PITCH_RANGES_PCT = (8.0, 16.0, 100.0)
#: Slack on the render's own deadline: a plan whose end never arrives stops
#: with an error at this multiple of its nominal length, not after four hours.
MAX_MS_FACTOR = 2.0
_EQ_BANDS = {"eq_low": "low", "eq_mid": "mid", "eq_high": "high"}


class RenderPlanError(ValueError):
    """The plan cannot be rendered as given (no tempo, too little track)."""


@dataclass(frozen=True)
class RenderLayout:
    """Where the render starts and how the incoming deck is tempo-matched."""

    start_out_bar: float
    start_out_s: float
    in_ratio: float
    in_fold: float
    end_in_bar: float
    out_bar_s: float

    def out_downbeats_render_s(self, out_profile: TrackProfile, until_s: float) -> list[float]:
        """The outgoing deck's downbeats on the render's clock (seconds from its start) up to ``until_s``.

        Past the end of the outgoing track's grid the bar line continues at its
        nominal bar length: the incoming deck's bars after the handover are
        still counted on the clock the plan was written in.
        """
        downs = [d - self.start_out_s for d in out_profile.downbeats_s if d >= self.start_out_s - 1e-9]
        if not downs:
            raise RenderPlanError(f"{out_profile.stable_id}: no downbeat at or after the render's start")
        while downs[-1] + self.out_bar_s <= until_s + 1e-9:
            downs.append(downs[-1] + self.out_bar_s)
        return [d for d in downs if d <= until_s + 1e-9]


def _bpm(profile: TrackProfile) -> float:
    if profile.bpm is None or profile.bpm <= 0:
        raise RenderPlanError(f"{profile.stable_id}: no tempo, so sync_to_out cannot be resolved to a ratio")
    return profile.bpm


def _pitch_range_for(ratio: float) -> float:
    for range_pct in PITCH_RANGES_PCT:
        if abs(ratio - 1.0) * 100.0 <= range_pct + 1e-9:
            return range_pct
    raise RenderPlanError(f"tempo ratio {ratio:.4f} is beyond every pitch range the engine has")


def layout_of(
    plan: TransitionPlan,
    out_profile: TrackProfile,
    in_profile: TrackProfile,
    context_bars: int = 4,
    *,
    start_out_bar: float | None = None,
) -> RenderLayout:
    """The render's start, the incoming tempo and the end bar, for ``plan``."""
    if context_bars < 1:
        raise RenderPlanError(f"context_bars must be at least 1, got {context_bars}")
    first_event = min((e.at_bar for e in plan.events), default=0.0)
    wanted = plan.out.start_bar + min(0.0, first_event) - context_bars
    start = wanted if start_out_bar is None else start_out_bar
    if start > wanted + 1e-9:
        raise RenderPlanError(f"start_out_bar {start} leaves less than {context_bars} bars of context before the plan")
    if start < 1.0:
        raise RenderPlanError(
            f"{out_profile.stable_id}: the plan starts at bar {plan.out.start_bar}, too early for "
            f"{context_bars} bars of context (the render would start before bar 1)"
        )
    if plan.tempo_mode == "sync_to_out":
        out_bpm, in_bpm = _bpm(out_profile), _bpm(in_profile)
        fold = pair_beat_sync_fold(out_bpm, in_bpm, BEATMATCH_LIMIT_PCT)
        ratio = out_bpm / in_bpm * fold
    elif plan.tempo_mode == "none":
        # Each deck runs at its own tempo, so an incoming bar is not an outgoing bar; the end is counted in
        # incoming bars below, which is only right when the two tempos agree.
        out_bpm, in_bpm = _bpm(out_profile), _bpm(in_profile)
        if not math.isclose(out_bpm, in_bpm, rel_tol=1e-6):
            raise RenderPlanError(
                f"an unsynced plan across {out_bpm:g} and {in_bpm:g} BPM: its incoming end is not mapped onto "
                "the outgoing clock yet"
            )
        fold, ratio = 1.0, 1.0
    else:
        raise RenderPlanError(f"unknown tempo mode {plan.tempo_mode!r}")
    end_in_bar = plan.in_.start_bar + (incoming_played_bars(plan) + context_bars) * fold
    if end_in_bar > in_profile.bars + 1:
        raise RenderPlanError(
            f"{in_profile.stable_id}: the render needs bar {end_in_bar:g} but the grid has {in_profile.bars} bars"
        )
    return RenderLayout(
        start_out_bar=start,
        start_out_s=out_profile.time_of_bar(start),
        in_ratio=ratio,
        in_fold=fold,
        end_in_bar=end_in_bar,
        out_bar_s=out_profile.bar_s,
    )


def beatgrid_of(profile: TrackProfile) -> list[dict[str, Any]]:
    """The engine's ``load.beatgrid``: four beats per bar between the profile's downbeats."""
    downbeats = profile.downbeats_s
    grid: list[dict[str, Any]] = []
    for index, start in enumerate(downbeats):
        end = downbeats[index + 1] if index + 1 < len(downbeats) else start + profile.bar_s
        grid.extend(
            {"n": beat + 1, "time_ms": round((start + (end - start) * beat / 4.0) * 1000.0, 3)} for beat in range(4)
        )
    return grid


def incoming_trim_command(plan: TransitionPlan) -> dict[str, Any] | None:
    """AGENT-31's loudness match, ``in.trim_db``, as the incoming TRIM knob at time 0 (the page sets it in prepare).

    The same ``trim_knob`` mapping ``apps.autodj.compile`` uses, so the render
    and the page play one level. None when the plan carries no ``trim_db``;
    its ``trim_reason`` then says why, and the try-er reports it with the
    candidate. An authored incoming trim would fight it, so that is refused.
    """
    if plan.in_.trim_db is None:
        return None
    if any(e.deck == plan.in_.deck and e.name == "trim" for e in plan.events):
        raise RenderPlanError("in.trim_db and an authored incoming trim event both set the trim: keep one")
    try:
        value = trim_knob(plan.in_.trim_db)
    except ValueError as error:
        raise RenderPlanError(str(error)) from error
    return {"type": "trim", "deck": plan.in_.deck, "value": value}


#: Plan controls the offline render applies, each with an engine command and DSP behind it
#: (tests/transition_score/test_render_path.py renders every one and measures the change).
#: The performance engine creates every deck with Master Tempo on (audio-engine.svelte.ts).
LIVE_MASTER_TEMPO = True
RENDERED_CONTROLS: frozenset[str] = frozenset({"fader", "trim", "filter", "eq_low", "eq_mid", "eq_high", "tempo"})


def _refuse_controls_the_render_cannot_apply(plan: TransitionPlan) -> None:
    """A control the render would play as a no-op is refused, never rendered silently.

    The crossfader is the case: its gain depends on each channel's A/B/THRU
    assignment (``mixer.rs`` ``xf_gain``), which lives on the live mixer, not in
    the plan; the offline decks load as THRU, where the crossfader does nothing,
    so a crossfader move would render as no change while the page plays a fade.
    """
    for event in plan.events:
        if event.kind == "do":
            continue
        if event.name not in RENDERED_CONTROLS:
            raise RenderPlanError(
                f"{event.name} at bar {event.at_bar:g} cannot be rendered offline: "
                + (
                    "the plan does not say each deck's crossfader assignment"
                    if event.name == "crossfader"
                    else "no engine command"
                )
            )


def _refuse_follower_tempo_moves(plan: TransitionPlan) -> None:
    """An authored incoming tempo move is refused in either tempo mode.

    Synced: live execution moves the outgoing leader for it (``apps.autodj.compile._leader_led_events``), and the
    offline render has no leader/follower clock, so it would move the incoming deck instead. Unsynced: the render's
    end is counted at the incoming deck's starting rate, so a later rate change ends it before the overlap does.
    """
    why = {
        "sync_to_out": "in a synced plan live execution moves the outgoing leader instead",
        "none": "the render end is counted at the incoming deck's starting rate",
    }[plan.tempo_mode]
    for event in plan.events:
        if event.kind != "do" and event.name == "tempo" and event.deck == plan.in_.deck:
            raise RenderPlanError(
                f"tempo on the incoming deck at bar {event.at_bar:g}: {why}, which the offline render cannot "
                "reproduce yet"
            )


def _value(event: PlanEvent) -> float:
    """A set's or ramp's target, validated by ``PlanEvent.target`` (a finite number, never coerced or defaulted)."""
    try:
        return event.target()
    except ValueError as error:
        raise RenderPlanError(f"{error} (deck {event.deck})") from error


def incoming_played_bars(plan: TransitionPlan) -> float:
    """Plan bars the incoming deck actually plays: from each effective play (``effective_bars``, its quantized launch)
    to the next stop or the plan's end, so a late launch or a pause never asks for audio the deck does not play."""
    bars = effective_bars(plan)
    steps = sorted(
        (bars[i], e.name == "play")
        for i, e in enumerate(plan.events)
        if e.kind == "do" and e.deck == plan.in_.deck and e.name in ("play", "stop")
    )
    played, since = 0.0, None
    for raw_at, playing in steps:
        at = min(raw_at, plan.bars)
        if playing and since is None:
            since = at
        elif not playing and since is not None:
            played, since = played + at - since, None
    return played + (plan.bars - since if since is not None else 0.0)


def effective_bars(plan: TransitionPlan) -> tuple[float, ...]:
    """The plan bar each of ``plan.events`` sounds at, index for index: a play starts where the executor's quantized
    launch does (``play_start_bar`` with the deck's previous play or stop, as compile passes it to ``launch_bar``),
    every other event at its own bar. Public: the render and the scorer's seams read this one timing."""
    bars: list[float] = []
    last_transport: dict[int | None, float] = {}
    for event in plan.events:
        is_play = event.kind == "do" and event.name == "play"
        bars.append(play_start_bar(event.at_bar, last_transport.get(event.deck)) if is_play else event.at_bar)
        if event.kind == "do" and event.name in ("play", "stop"):
            last_transport[event.deck] = event.at_bar
    return tuple(bars)


def _command(event: PlanEvent) -> dict[str, Any]:
    if event.kind == "do":
        assert event.deck is not None
        return {"type": "play", "deck": event.deck, "playing": event.name == "play"}
    value = _value(event)
    if event.name == "crossfader":
        return {"type": "crossfader", "value": value}
    if event.name in _EQ_BANDS:
        return {"type": "eq", "deck": event.deck, "band": _EQ_BANDS[event.name], "value": value}
    if event.name == "tempo":
        return {"type": "tempo", "deck": event.deck, "ratio": value}
    return {"type": event.name, "deck": event.deck, "value": value}


def _ramp(event: PlanEvent, clock_deck: int) -> dict[str, Any]:
    value = _value(event)
    knob: dict[str, Any] = {"type": "crossfader"} if event.name == "crossfader" else {"deck": event.deck}
    if event.name in _EQ_BANDS:
        knob = {"type": "eq", "deck": event.deck, "band": _EQ_BANDS[event.name]}
    elif event.name != "crossfader":
        knob["type"] = event.name
    return {**knob, "to": value, "over": {"bars": event.over_bars, "deck": clock_deck}}


def renders_at_start(event: PlanEvent, plan: TransitionPlan, layout: RenderLayout) -> bool:
    """Whether ``event`` is applied at render time zero rather than at its bar: it falls in or before the context
    start, or it is an incoming set live compile hoists into ``prepare``."""
    return plan.out.start_bar + event.at_bar <= layout.start_out_bar or hoistable(event, plan)


def compile_render_plan(
    plan: TransitionPlan,
    out_profile: TrackProfile,
    in_profile: TrackProfile,
    context_bars: int = 4,
    *,
    out_audio: Path,
    in_audio: Path,
    start_out_bar: float | None = None,
) -> dict[str, Any]:
    """The ``plan.rs`` JSON that renders ``plan`` with ``context_bars`` of each deck alone around it."""
    layout = layout_of(plan, out_profile, in_profile, context_bars, start_out_bar=start_out_bar)
    _refuse_controls_the_render_cannot_apply(plan)
    _refuse_follower_tempo_moves(plan)
    for audio in (out_audio, in_audio):
        if not audio.is_absolute():
            # The renderer resolves a relative load against the plan file's own directory (offline.rs), not the
            # caller's, so it would decode a different file from the one the caller named.
            raise RenderPlanError(f"audio path {audio} must be absolute")
    out_deck, in_deck = plan.out.deck, plan.in_.deck
    at0 = {"ms": 0}
    events: list[dict[str, Any]] = [
        {
            "at": at0,
            "cmd": {"type": "load", "deck": out_deck, "path": str(out_audio), "beatgrid": beatgrid_of(out_profile)},
        },
        {
            "at": at0,
            "cmd": {"type": "load", "deck": in_deck, "path": str(in_audio), "beatgrid": beatgrid_of(in_profile)},
        },
        {"at": at0, "cmd": {"type": "seek", "deck": out_deck, "position_ms": round(layout.start_out_s * 1000.0, 3)}},
        {
            "at": at0,
            "cmd": {
                "type": "seek",
                "deck": in_deck,
                "position_ms": round(in_profile.time_of_bar(plan.in_.start_bar) * 1000.0, 3),
            },
        },
        # Live decks default Master Tempo on (AGENTS.md); the Rust deck defaults it off, so it is set here.
        {"at": at0, "cmd": {"type": "master_tempo", "deck": out_deck, "enabled": LIVE_MASTER_TEMPO}},
        {"at": at0, "cmd": {"type": "master_tempo", "deck": in_deck, "enabled": LIVE_MASTER_TEMPO}},
        {"at": at0, "cmd": {"type": "pitch_range", "deck": in_deck, "range": _pitch_range_for(layout.in_ratio)}},
        {"at": at0, "cmd": {"type": "tempo", "deck": in_deck, "ratio": round(layout.in_ratio, 9)}},
        {"at": at0, "cmd": {"type": "play", "deck": out_deck, "playing": True}},
    ]
    trim = incoming_trim_command(plan)
    if trim is not None:
        events.insert(len(events) - 1, {"at": at0, "cmd": trim})
    timed: list[tuple[float, int, dict[str, Any]]] = []
    bars = effective_bars(plan)
    for index, event in enumerate(plan.events):
        if event.kind == "do" and event.name == "master":
            continue
        out_bar = plan.out.start_bar + bars[index]
        # Live compile runs a hoistable incoming set in ``prepare``, before anything plays; here that is render
        # time zero, so the idle strip settles during the outgoing context rather than on the incoming play's frame.
        early = renders_at_start(event, plan, layout)
        at = at0 if early else {"deck": out_deck, "bar": round(out_bar, 9)}
        body = {"ramp": _ramp(event, out_deck)} if event.kind == "ramp" else {"cmd": _command(event)}
        timed.append((out_bar, index, {"at": at, **body}))
    events += [row for _, _, row in sorted(timed, key=lambda item: (item[0], item[1]))]
    # The whole render clock: outgoing pre-context, every plan bar (a late launch or a pause waits on this clock too),
    # then the incoming bars still to play after the plan, converted to outgoing bars.
    after_plan_in_bars = layout.end_in_bar - plan.in_.start_bar - incoming_played_bars(plan) * layout.in_fold
    out_bars = (plan.out.start_bar - layout.start_out_bar) + plan.bars + after_plan_in_bars / layout.in_fold
    nominal_ms = out_bars * layout.out_bar_s * 1000.0
    return {
        "sample_rate": RENDER_SAMPLE_RATE,
        "max_ms": math.ceil(nominal_ms * MAX_MS_FACTOR),
        "end": {"deck": in_deck, "bar": round(layout.end_in_bar, 9)},
        "events": events,
    }
