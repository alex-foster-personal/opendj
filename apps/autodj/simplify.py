"""A hand mix simplified into a :class:`~apps.autodj.plan.TransitionPlan` (MEMORY-05, ADR-0129).

Pure: :func:`simplify` takes the control lanes one overlap recorded and returns a plan, so the fit is pinned by golden
tests and the capture hook (:mod:`apps.autodj.capture`) only feeds it.

The steps, per lane: Ramer-Douglas-Peucker at ``KNOB_TOLERANCE``, which drops hand jitter and keeps every real change
of slope; breakpoints snapped to a beat (``BEAT_BARS``); values snapped to a knob's rail or detent when they sit within
the tolerance of one; each remaining segment becomes a ``ramp``, or a ``set`` when it is no longer than ``JUMP_BARS``,
which is what a step between two mirror samples looks like. A lane whose whole range is within twice the tolerance only
jittered and leaves no event.

Plan bar 0 is the bar of the first move, so cueing in the headphones before the mix is not part of the plan.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from itertools import pairwise
from typing import Any, NamedTuple

from apps.autodj.plan import (
    FLAT,
    PLAY_LEAD_BARS,
    SHAPE_STYLES,
    PlanEvent,
    TransitionPlan,
    _value_at,
    parse_plan,
)
from apps.autodj.styles import STYLES, handover_events

#: How far (knob units, 0..1) a fitted line may sit from the hand's samples; also the rail-snap distance.
KNOB_TOLERANCE = 0.03
#: Every breakpoint lands on a beat of the 4/4 plan clock.
BEAT_BARS = 0.25
#: A segment this short is a jump: the mirror publishes once a second, about half a bar at 120 BPM.
JUMP_BARS = 0.5
#: How far a catalog style's lane may sit from the fitted lane, at any beat, for the mix to take its name. The fitted
#: lane may also trail the style by JUMP_BARS: a style's half-bar edge comes back from the mirror as a late jump.
STYLE_MATCH_TOLERANCE = 0.15
#: A mix that matches no catalog style. validate_plan warns on it and takes its events as written.
CAPTURED_STYLE = "captured"
RATIONALE = "captured from a hand mix"

#: The knob positions a value snaps to: closed, flat or center, full.
_RAILS = (0.0, FLAT, 1.0)
_VALUE_PLACES = 3

Lane = tuple[int | None, str]


class SimplifyError(ValueError):
    """The overlap holds no transition to save."""


class Sample(NamedTuple):
    at_bar: float
    value: float


@dataclass(frozen=True)
class CapturedMix:
    """One overlap as recorded: ``at_bar`` counts from the outgoing deck's bar ``out_start_bar``."""

    out_deck: int
    out_track: str
    out_start_bar: int
    in_deck: int
    in_track: str
    #: The incoming grid bar sounding at plan bar 0.
    in_start_bar: int
    #: Where the outgoing deck went quiet or stopped.
    end_bar: float
    lanes: Mapping[Lane, tuple[Sample, ...]]
    tempo_mode: str


# ----- one lane ------------------------------------------------------------


def _offset(point: Sample, start: Sample, stop: Sample) -> float:
    """How far ``point`` sits from the line through ``start`` and ``stop``, in knob units."""
    span = stop.at_bar - start.at_bar
    if span <= 0:
        return abs(point.value - start.value)
    return abs(point.value - (start.value + (stop.value - start.value) * (point.at_bar - start.at_bar) / span))


def _douglas_peucker(points: list[Sample], tolerance: float) -> list[Sample]:
    if len(points) < 3:
        return points
    offsets = [_offset(point, points[0], points[-1]) for point in points[1:-1]]
    worst = max(offsets)
    if worst <= tolerance:
        return [points[0], points[-1]]
    split = offsets.index(worst) + 1
    return [*_douglas_peucker(points[: split + 1], tolerance)[:-1], *_douglas_peucker(points[split:], tolerance)]


def _snap_bar(at_bar: float) -> float:
    return round(at_bar / BEAT_BARS) * BEAT_BARS


def _snap_value(value: float) -> float:
    rail = min(_RAILS, key=lambda candidate: abs(candidate - value))
    return rail if abs(rail - value) <= KNOB_TOLERANCE else round(value, _VALUE_PLACES)


def _moved(samples: tuple[Sample, ...]) -> bool:
    values = [sample.value for sample in samples]
    return max(values) - min(values) > 2 * KNOB_TOLERANCE


def _breakpoints(samples: tuple[Sample, ...]) -> list[Sample]:
    """The lane as beat-snapped breakpoints; two on one beat keep the later value."""
    snapped: dict[float, float] = {}
    for point in _douglas_peucker(list(samples), KNOB_TOLERANCE):
        snapped[_snap_bar(point.at_bar)] = _snap_value(point.value)
    return [Sample(at_bar, value) for at_bar, value in sorted(snapped.items())]


def _segment_events(lane: Lane, points: list[Sample]) -> list[dict[str, Any]]:
    deck, control = lane
    events: list[dict[str, Any]] = []
    for start, stop in pairwise(points):
        if abs(stop.value - start.value) <= KNOB_TOLERANCE:
            continue
        span = stop.at_bar - start.at_bar
        if span <= JUMP_BARS:
            events.append({"at_bar": stop.at_bar, "deck": deck, "set": {control: stop.value}})
        else:
            events.append({"at_bar": start.at_bar, "deck": deck, "ramp": {control: stop.value}, "over_bars": span})
    return events


# ----- the plan ------------------------------------------------------------


def _neutral(control: str) -> float:
    return 1.0 if control == "fader" else FLAT


def _preset(lane: Lane, value: float) -> dict[str, Any]:
    """A value in place when the launch is dispatched: compile hoists an incoming set into prepare."""
    deck, control = lane
    return {"at_bar": -PLAY_LEAD_BARS, "deck": deck, "set": {control: value}}


def _lane_events(
    lane: Lane, samples: tuple[Sample, ...], in_deck: int
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """A lane's timed events, and the preset that seeds it, if it has one."""
    deck, control = lane
    incoming = deck == in_deck
    if incoming and control == "trim":  # validate_plan refuses a trim moved while the deck is heard, so it lands early
        value = _snap_value(samples[-1].value)
        return [], _preset(lane, value)
    if not _moved(samples):
        value = _snap_value(samples[0].value)
        return [], _preset(lane, value) if incoming else None
    points = _breakpoints(samples)
    seeded = incoming or deck is None
    return _segment_events(lane, points), _preset(lane, points[0].value) if seeded else None


def _shifted(events: list[dict[str, Any]], lead: float) -> list[dict[str, Any]]:
    return [{**event, "at_bar": event["at_bar"] - lead} for event in events]


def _document(mix: CapturedMix, events: list[dict[str, Any]], *, bars: int, lead: int, style: str) -> dict[str, Any]:
    return {
        "out": {
            "deck": mix.out_deck,
            "track": mix.out_track,
            "window": {"start_bar": mix.out_start_bar + lead, "bars": bars},
        },
        "in": {
            "deck": mix.in_deck,
            "track": mix.in_track,
            "window": {"start_bar": mix.in_start_bar + lead, "bars": bars},
        },
        "anchor": "window",
        "style": style,
        "tempo": {"mode": mix.tempo_mode},
        "events": events,
        "intent": None,
        "rationale": RATIONALE,
    }


def _curve(events: tuple[PlanEvent, ...], lane: Lane) -> Callable[[float], float]:
    deck, control = lane
    ordered = list(events)
    return lambda at_bar: _value_at(ordered, deck, control, at_bar, _neutral(control))


def _active_lanes(events: tuple[PlanEvent, ...], bars: int) -> dict[Lane, Callable[[float], float]]:
    """The lanes that leave neutral inside the plan: the handover's own resets are not part of a mix's shape."""
    timed = tuple(event for event in events if event.at_bar < bars)
    lanes = {(event.deck, event.name) for event in timed if event.kind != "do"}
    curves = {lane: _curve(timed, lane) for lane in lanes}
    beats = [step * BEAT_BARS for step in range(int(bars / BEAT_BARS) + 1)]
    return {
        lane: curve
        for lane, curve in curves.items()
        if any(abs(curve(beat) - _neutral(lane[1])) > KNOB_TOLERANCE for beat in beats)
    }


def _matches(plan_events: tuple[PlanEvent, ...], style_events: tuple[PlanEvent, ...], bars: int) -> bool:
    ours, theirs = _active_lanes(plan_events, bars), _active_lanes(style_events, bars)
    if not ours or set(ours) != set(theirs):
        return False
    beats = [step * BEAT_BARS for step in range(int(bars / BEAT_BARS))]
    # Use an explicit tolerance: a 0.0 min diff is a perfect match but falsy in ``all()``.
    return all(
        min(abs(_curve_value(ours[lane], beat) - _curve_value(theirs[lane], beat - lag)) for lag in (0.0, JUMP_BARS))
        <= STYLE_MATCH_TOLERANCE
        for lane in ours
        for beat in beats
    )


def _curve_value(curve: Callable[[float], float], beat: float) -> float:
    """Call a selected lane through a named seam the static import reader can follow."""
    return curve(beat)


def _named_style(document: dict[str, Any], plan: TransitionPlan) -> str:
    """The catalog style this shape is, or :data:`CAPTURED_STYLE`. A style with an intended shape never matches: its
    plans carry that shape, and a mix that happens to look like one did not claim it."""
    for style in STYLES.values():
        fits = style.available and style.builder is not None and style.name not in SHAPE_STYLES.values()
        if fits and style.min_bars <= plan.bars <= style.max_bars:
            events = style.build(plan.bars, plan.out.deck, plan.in_.deck)
            if _matches(plan.events, parse_plan({**document, "events": events}).events, plan.bars):
                return style.name
    return CAPTURED_STYLE


def simplify(mix: CapturedMix) -> TransitionPlan:
    """The plan for one overlap, parsed by ``plan.py``. :class:`SimplifyError` when no control moved."""
    timed: list[dict[str, Any]] = []
    presets: list[dict[str, Any]] = []
    for lane, samples in sorted(mix.lanes.items(), key=lambda item: (item[0][0] or 0, item[0][1])):
        kept = tuple(sample for sample in samples if sample.at_bar <= mix.end_bar)
        if kept:
            events, preset = _lane_events(lane, kept, mix.in_deck)
            timed.extend(events)
            presets.extend([] if preset is None else [preset])
    if not timed:
        raise SimplifyError("no control moved in the overlap")
    lead = math.floor(min(event["at_bar"] for event in timed))
    bars = max(1, math.ceil(mix.end_bar - lead))
    launch = {"at_bar": 0, "deck": mix.in_deck, "do": "play"}
    events = [*presets, launch, *_shifted(timed, lead), *handover_events(bars, mix.out_deck, mix.in_deck)]
    document = _document(mix, events, bars=bars, lead=lead, style=CAPTURED_STYLE)
    parsed = parse_plan(document)
    return parse_plan({**document, "style": _named_style(document, parsed)})
