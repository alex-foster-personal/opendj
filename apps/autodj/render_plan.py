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
from pathlib import Path
from typing import Any

from apps.autodj.contract.models import RenderLayout, RenderPlanError
from apps.autodj.plan import (
    FX_ACTIONS,
    FX_CONTROLS,
    STEM_CONTROLS,
    PlanEvent,
    TransitionPlan,
    _transport_steps,
    beat_sync_reach_pct,
    glide_span,
    hoistable,
    pair_beat_sync_fold,
    play_start_bar,
    surviving_transport,
)
from apps.autodj.profile import DECK_LOAD_TRIM_DB, TrackProfile, trim_knob

#: The Rust engine's own reason (apps/audio-engine/src/protocol.rs TRANSITION_FX_LATER, FXTRANS-03).
TRANSITION_FX_REFUSAL = "transition FX are Web Audio only until their kernels are ported (FXTRANS-03)"

#: The renderer's rate; 48 kHz matches the engine's default and the page.
RENDER_SAMPLE_RATE = 48000
#: Pitch ranges the engine accepts (deck.rs ``set_pitch_range``).
PITCH_RANGES_PCT = (8.0, 16.0, 100.0)
#: Slack on the render's own deadline: a plan whose end never arrives stops
#: with an error at this multiple of its nominal length, not after four hours.
MAX_MS_FACTOR = 2.0
_EQ_BANDS = {"eq_low": "low", "eq_mid": "mid", "eq_high": "high"}


def _bpm(profile: TrackProfile) -> float:
    if profile.bpm is None or profile.bpm <= 0:
        raise RenderPlanError(f"{profile.stable_id}: no tempo, so sync_to_out cannot be resolved to a ratio")
    return profile.bpm


def _judged_bpm(profile: TrackProfile) -> float:
    value = profile.judged_bpm
    if value is None or value <= 0:
        raise RenderPlanError(f"{profile.stable_id}: no heard or stored tempo to judge the pair with")
    return float(value)


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
    out_stored, in_stored = _bpm(out_profile), _bpm(in_profile)
    out_j, in_j = _judged_bpm(out_profile), _judged_bpm(in_profile)
    out_ratio = out_j / out_stored
    if plan.tempo_mode in ("sync_to_out", "glide_to_in"):
        fold = pair_beat_sync_fold(out_j, in_j, beat_sync_reach_pct(in_profile))
        ratio = out_j / in_j * fold
    elif plan.tempo_mode == "none":
        # The incoming deck plays at its own rate (ratio 1.0); fold maps incoming grid bars per outgoing plan bar.
        fold = in_stored / (out_stored * out_ratio)
        ratio = 1.0
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
        out_ratio=out_ratio,
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
STEM_RENDER_REFUSAL = "stems are not in the offline renderer (plan 20-04)"
RENDERED_CONTROLS: frozenset[str] = frozenset({"fader", "trim", "filter", "eq_low", "eq_mid", "eq_high", "tempo"})


def _refuse_controls_the_render_cannot_apply(plan: TransitionPlan) -> None:
    """A control the render would play as a no-op is refused, never rendered silently.

    The crossfader is the case: its gain depends on each channel's A/B/THRU
    assignment (``mixer.rs`` ``xf_gain``), which lives on the live mixer, not in
    the plan; the offline decks load as THRU, where the crossfader does nothing,
    so a crossfader move would render as no change while the page plays a fade.
    """
    for event in plan.events:
        if event.name in FX_CONTROLS or event.name in FX_ACTIONS:
            raise RenderPlanError(
                f"{event.name} at bar {event.at_bar:g} cannot be rendered offline: {TRANSITION_FX_REFUSAL}"
            )
        if event.name in STEM_CONTROLS:
            raise RenderPlanError(
                f"{event.name} at bar {event.at_bar:g} cannot be rendered offline: {STEM_RENDER_REFUSAL}"
            )
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
        "glide_to_in": "in a synced plan live execution moves the outgoing leader instead",
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
    """Plan bars the incoming deck actually plays: from each surviving play's quantized launch
    to the next stop or the plan's end, so a late launch or a pause never asks for audio the deck does not play."""
    played, since = 0.0, None
    for raw_at, playing in _transport_steps(plan.events, plan.in_.deck):
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


def glide_ramps(plan: TransitionPlan, layout: RenderLayout) -> list[dict[str, Any]]:
    """#10148: a glide_to_in plan's glide as explicit per-bar tempo ramps on BOTH decks (``plan.rs`` has no leader):
    the incoming deck from its synced ratio to 1.0 and the outgoing one by the same factor, so they stay locked and
    the incoming ends at its own tempo. Empty for any other mode."""
    span = glide_span(plan)
    if span is None:
        return []
    start, end = span
    factor = 1.0 / layout.in_ratio
    rows: list[dict[str, Any]] = []
    bar = start
    while bar < end - 1e-9:
        step = min(1.0, end - bar)
        done = (bar + step - start) / (end - start)
        for deck, ratio in ((plan.out.deck, layout.out_ratio), (plan.in_.deck, layout.in_ratio)):
            to = round(ratio * (1.0 + (factor - 1.0) * done), 9)
            ramp = {"type": "tempo", "deck": deck, "to": to, "over": {"bars": round(step, 9), "deck": plan.out.deck}}
            rows.append({"at": {"deck": plan.out.deck, "bar": round(plan.out.start_bar + bar, 9)}, "ramp": ramp})
        bar += step
    return rows


def _require_absolute_audio_paths(*audio: Path) -> None:
    for path in audio:
        if not path.is_absolute():
            # The renderer resolves a relative load against the plan file's own directory (offline.rs), not the
            # caller's, so it would decode a different file from the one the caller named.
            raise RenderPlanError(f"audio path {path} must be absolute")


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
    _refuse_controls_the_render_cannot_apply(plan)
    if plan.in_.key_shift_semitones:
        # #10142: plan.rs has no key shift command yet; never drop the shift silently.
        raise RenderPlanError("static key shift not rendered yet: odj-audio render takes no key_shift")
    _refuse_follower_tempo_moves(plan)
    layout = layout_of(plan, out_profile, in_profile, context_bars, start_out_bar=start_out_bar)
    _require_absolute_audio_paths(out_audio, in_audio)
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
    ]
    glide = glide_ramps(plan, layout)
    out_end = layout.out_ratio / layout.in_ratio if glide else layout.out_ratio
    if not math.isclose(layout.out_ratio, 1.0, rel_tol=0.0, abs_tol=1e-9) or glide:
        r = round(layout.out_ratio, 9)
        reach = max(r, out_end, key=lambda value: abs(value - 1.0))
        events.extend(
            [
                {"at": at0, "cmd": {"type": "pitch_range", "deck": out_deck, "range": _pitch_range_for(reach)}},
                {"at": at0, "cmd": {"type": "tempo", "deck": out_deck, "ratio": r}},
            ]
        )
    for deck, profile in ((out_deck, out_profile), (in_deck, in_profile)):
        shift = profile.heard_key_shift
        if shift == 0:
            continue
        if not isinstance(shift, int) or shift < -12 or shift > 12:
            raise RenderPlanError(
                f"{profile.stable_id}: heard_key_shift {shift!r} is not an integer from -12 to 12 for key_nudge"
            )
        events.append({"at": at0, "cmd": {"type": "key_nudge", "deck": deck, "semitones": shift}})
    events.extend(
        [
            {"at": at0, "cmd": {"type": "pitch_range", "deck": in_deck, "range": _pitch_range_for(layout.in_ratio)}},
            {"at": at0, "cmd": {"type": "tempo", "deck": in_deck, "ratio": round(layout.in_ratio, 9)}},
            {"at": at0, "cmd": {"type": "play", "deck": out_deck, "playing": True}},
        ]
    )
    # ADR-0135 (opt-in since round 22): both decks get the non-unity load trim at time zero, before the outgoing deck
    # plays, so it is never a step on a playing deck. A matched incoming trim replaces that deck's load trim.
    if DECK_LOAD_TRIM_DB != 0.0:
        events.insert(
            len(events) - 1,
            {"at": at0, "cmd": {"type": "trim", "deck": out_deck, "value": trim_knob(DECK_LOAD_TRIM_DB)}},
        )
    trim = incoming_trim_command(plan)
    if trim is not None:
        events.insert(len(events) - 1, {"at": at0, "cmd": trim})
    elif DECK_LOAD_TRIM_DB != 0.0:
        events.insert(
            len(events) - 1,
            {"at": at0, "cmd": {"type": "trim", "deck": in_deck, "value": trim_knob(DECK_LOAD_TRIM_DB)}},
        )
    timed: list[tuple[float, int, dict[str, Any]]] = []
    bars = effective_bars(plan)
    kept = {id(event) for deck in (plan.out.deck, plan.in_.deck) for event in surviving_transport(plan.events, deck)}
    for index, event in enumerate(plan.events):
        if event.kind == "do" and event.name == "master":
            continue
        if event.kind == "do" and event.name in ("play", "stop") and id(event) not in kept:
            continue
        out_bar = plan.out.start_bar + bars[index]
        # Live compile runs a hoistable incoming set in ``prepare``, before anything plays; here that is render
        # time zero, so the idle strip settles during the outgoing context rather than on the incoming play's frame.
        early = renders_at_start(event, plan, layout)
        at = at0 if early else {"deck": out_deck, "bar": round(out_bar, 9)}
        body = {"ramp": _ramp(event, out_deck)} if event.kind == "ramp" else {"cmd": _command(event)}
        timed.append((out_bar, index, {"at": at, **body}))
    timed.extend((row["at"]["bar"], len(plan.events) + i, row) for i, row in enumerate(glide))
    events += [row for _, _, row in sorted(timed, key=lambda item: (item[0], item[1]))]
    # The whole render clock: outgoing pre-context, every plan bar (a late launch or a pause waits on this clock too),
    # then the incoming bars still to play after the plan, converted to outgoing bars.
    after_plan_in_bars = layout.end_in_bar - plan.in_.start_bar - incoming_played_bars(plan) * layout.in_fold
    out_bars = (plan.out.start_bar - layout.start_out_bar) + plan.bars + after_plan_in_bars / layout.in_fold
    nominal_ms = out_bars * (layout.out_bar_s / layout.out_ratio) * 1000.0
    return {
        "sample_rate": RENDER_SAMPLE_RATE,
        "max_ms": math.ceil(nominal_ms * MAX_MS_FACTOR),
        "end": {"deck": in_deck, "bar": round(layout.end_in_bar, 9)},
        "events": events,
    }
