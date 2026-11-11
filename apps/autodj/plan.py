"""TransitionPlan: the canon doc's plan, as data, plus ``validate_plan``.

The wire shape is the canon doc's example::

    {
      "out": {"deck": 1, "track": "<stable_id>", "window": {"start_bar": 161, "bars": 32}},
      "in":  {"deck": 2, "track": "<stable_id>", "window": {"start_bar": 17, "bars": 32}},
      "style": "bass_swap_blend",
      "tempo": {"mode": "sync_to_out"},
      "events": [
        {"at_bar": 0,  "deck": 2, "do": "play"},
        {"at_bar": 0,  "deck": 2, "ramp": {"fader": 1.0}, "over_bars": 8},
        {"at_bar": 16, "deck": 1, "set":  {"eq_low": 0.0}}
      ],
      "intent": "keep the energy up",
      "rationale": "..."
    }

``at_bar`` counts bars on the PLAN's clock: bar 0 is the outgoing window's
first bar (``out.window.start_bar`` on the outgoing track's own grid), and the
plan ends ``out.window.bars`` bars later. The incoming deck enters at
``in.window.start_bar`` when its ``play`` event fires. Each event does exactly
one of ``do`` (play, stop, master), ``set`` or ``ramp`` (one control each).

:func:`simulate` replays the events bar by bar, which is what lets
:func:`validate_plan` measure the clashes the canon doc names (two basslines,
two vocals) symbolically, before anything is rendered or played.
"""

from __future__ import annotations

import json
import math
from bisect import bisect_right
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from functools import partial
from itertools import pairwise
from typing import Any, NamedTuple, TypeVar

from apps.autodj.contract.models import (
    BassEntry,
    PlanError,
    PlanEvent,
    Side,
    TransitionPlan,
)
from apps.autodj.profile import DECK_LOAD_TRIM_DB, TRIM_MAX_DB, VOCAL_BAR_THRESHOLD, TrackProfile, trim_knob
from apps.autodj.styles import STYLES

#: Spec 09-transitions-fx 4.2: the outgoing deck's color ECHO send and its dry level (0..1), and the action that
#: puts the effect, its knob and the dry back to their pre-plan values.
#: #10246: fx_noise rides the color NOISE (level 0..1 as the PARAMETER, sweep -1..1 as the COLOR knob).
FX_CONTROLS: frozenset[str] = frozenset({"fx_echo", "fx_dry", "fx_noise"})
FX_ACTIONS: frozenset[str] = frozenset({"fx_reset"})
#: fx_noise's fields, in the order one event splits into.
NOISE_FIELDS: tuple[str, ...] = ("level", "sweep")
#: #9848 empties this when compile.py and agent-plan.ts carry the fx controls. fx_noise is compiled and the page
#: parses it, refusing it with its own reason until NOISE exists (#9804), so live validation does not name it here;
#: a plan with fx_noise still carries fx_reset, which stays pending on #9848.
PAGE_PENDING_FX: frozenset[str] = frozenset({"fx_echo", "fx_dry", "fx_reset"})
#: Per-deck controls a plan may set or ramp, and the bus command each becomes.
DECK_CONTROLS: frozenset[str] = frozenset({"fader", "trim", "filter", "eq_low", "eq_mid", "eq_high", "tempo"}) | (
    FX_CONTROLS
)
#: Mixer-wide controls (no deck).
MIXER_CONTROLS: frozenset[str] = frozenset({"crossfader"})
CONTROLS: frozenset[str] = DECK_CONTROLS | MIXER_CONTROLS
ACTIONS: frozenset[str] = frozenset({"play", "stop", "master"}) | FX_ACTIONS
TEMPO_MODES: frozenset[str] = frozenset({"sync_to_out", "none"})
ANCHORS: frozenset[str] = frozenset({"window", "next_phrase", "next_bar"})
#: D13: plan shapes whose loudness dip before the incoming bass hit is intended (a scorer may skip its loudness-loss
#: check for them); every other plan carries none.
SHAPES: frozenset[str] = frozenset({"lull_then_drop"})
#: The style each shape is built by: the shape is explicit on the plan, never implied by its style alone.
SHAPE_STYLES: Mapping[str, str] = {"lull_then_drop": "lull_then_drop"}

#: How far ahead of the outgoing playhead a plan's first event must be for it to still schedule.
OUT_POSITION_MARGIN_BARS = 0.5

#: Beat Sync's reach (``apps/shared/harmonic.py`` pitch window, the canon doc's 6%).
BEATMATCH_LIMIT_PCT = 6.0
#: How far before bar 0 an event may sit (a quantized launch lead).
EARLIEST_BAR = -1.0
#: A quantized launch fires on the master's next downbeat, so a play sent one beat ahead lands on the plan's bar.
PLAY_LEAD_BARS = 0.25
#: How deep an ``extra`` extension value may nest. Far above any real extension, far below every encoder's stack,
#: so a plan that parses always prints, encodes and sends.
EXTRA_MAX_DEPTH = 32
#: Fader and EQ above these count as "the deck is heard" in the simulation.
AUDIBLE_FADER = 0.3
#: ADR-0135: the trim every deck loads at (apps.autodj.profile.DECK_LOAD_TRIM_DB), as a gain.
LOAD_GAIN = 10.0 ** (DECK_LOAD_TRIM_DB / 20.0)
#: ADR-0135: the trim every deck loads at, as the page's TRIM knob value.
LOAD_TRIM = trim_knob(DECK_LOAD_TRIM_DB)
# The page's silence threshold on a channel's gain (agent-plan.ts SILENCE_GAIN_EPSILON): below it a channel is silent.
SILENT_GAIN = 1e-4
#: Samples per bar on the grid that silence is checked on before the flips inside it are bisected.
SILENCE_SAMPLES_PER_BAR = 32
#: Samples per bar inside a ramp's span: a ramp crosses the audible floor where its value does, so its span is finer.
SILENCE_SAMPLES_PER_RAMP_BAR = 512
#: Bisection steps that place an audibility flip to well under a sample of a bar.
BOUNDARY_BISECTIONS = 40
#: Silent stretches no longer than this (in bars) are bisection rounding, not silence the executor would honor.
SILENCE_EPSILON_BARS = 1e-9
STRONG_BASS = 0.5
FLAT = 0.5
#: D14: two basslines clash only when both lows are NEAR FULL: within 3 dB of flat on the EQ law (0.5 flat, 0 = -26 dB,
#: linear in dB). the maintainer's crossover keeps both partly present for a few bars on purpose; that is not a clash.
NEAR_FULL_LOW = round(FLAT - FLAT * 3.0 / 26.0, 6)
#: The crossfader's middle, and how far toward the other side a channel stays heard: a
#: side-A channel is cut at 0.9 and up, a side-B channel at 0.1 and down.
CROSSFADER_CENTER = 0.5
CROSSFADER_CLOSED = 0.9
#: The TRIM knob's unity position (``mixer-types.ts``: 0..1, 0.5 = unity). The engine
#: scales gain linearly by it, so a channel left at trim 0 is silent whatever its fader.
UNITY_TRIM = 0.5
#: Beat Sync's tempo candidates, in its own order (``beat-sync-math.ts``
#: ``_tempoRatioWithinRangeOrNull``): the raw ratio, then half time, then double time.
TEMPO_FOLDS = (1.0, 0.5, 2.0)


_T = TypeVar("_T")


def _in_field(path: str, parse: Callable[[], _T]) -> _T:
    """``parse()``, with ``path`` stamped on a PlanError that names no field yet."""
    try:
        return parse()
    except PlanError as error:
        if error.path is None:
            error.path = path
        raise


# ----- parsing -------------------------------------------------------------


def _int(value: Any, where: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PlanError(f"{where} must be an integer, got {value!r}")
    if minimum is not None and value < minimum:
        raise PlanError(f"{where} must be >= {minimum}, got {value}")
    return value


def _number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PlanError(f"{where} must be a number, got {value!r}")
    if isinstance(value, float) and not math.isfinite(value):
        raise PlanError(f"{where} must be finite, got {value!r}")
    try:
        return float(value)
    except OverflowError as error:  # a huge integer, such as 10**400, has no float
        raise PlanError(f"{where} must be finite, got {value!r}") from error


def _deck(value: Any, where: str) -> int:
    deck = _int(value, where)
    if deck not in (1, 2, 3, 4):
        raise PlanError(f"{where} must be a deck 1..4, got {deck}")
    return deck


def _trim_reason(value: Any, where: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise PlanError(f"{where}.trim_reason must be text or null")
    return value


def _side(document: Any, where: str) -> Side:
    if not isinstance(document, Mapping):
        raise PlanError(f"{where} must be an object with deck, track and window")
    track = document.get("track")
    if not isinstance(track, str) or not track:
        raise PlanError(f"{where}.track must be a stable_id")
    window = document.get("window")
    if not isinstance(window, Mapping):
        raise PlanError(f"{where}.window must be an object with start_bar and bars")
    trim_db = document.get("trim_db")
    if trim_db is not None and (
        not isinstance(trim_db, (int, float)) or isinstance(trim_db, bool) or not math.isfinite(trim_db)
    ):
        raise PlanError(f"{where}.trim_db must be a number of dB or null")
    if trim_db is not None and where != "in":
        raise PlanError("only the incoming side carries trim_db: the outgoing track is the loudness reference")
    return Side(
        deck=_deck(document.get("deck"), f"{where}.deck"),
        track=track,
        start_bar=_int(window.get("start_bar"), f"{where}.window.start_bar", minimum=1),
        bars=_int(window.get("bars"), f"{where}.window.bars", minimum=1),
        trim_db=None if trim_db is None else float(trim_db),
        trim_reason=_trim_reason(document.get("trim_reason"), where),
    )


def _member(value: Any, allowed: frozenset[str], where: str) -> str:
    """A text value from ``allowed``. Any other JSON type is a plan error, never an unhashable-value crash."""
    if not isinstance(value, str) or value not in allowed:
        raise PlanError(f"{where} must be one of {sorted(allowed)}, got {value!r}")
    return value


def _single(mapping: Any, where: str) -> tuple[str, Any]:
    if not isinstance(mapping, Mapping) or len(mapping) != 1:
        raise PlanError(f'{where} must name exactly one control, e.g. {{"fader": 1.0}}')
    ((name, value),) = mapping.items()
    if name not in CONTROLS:
        raise PlanError(f"{where} control {name!r} is not one of {sorted(CONTROLS)}")
    if name == "fx_noise":
        return str(name), value
    return str(name), _number(value, f"{where}.{name}")


def _noise_fields(value: Any, where: str) -> list[tuple[str, float]]:
    """fx_noise's ``{level, sweep}`` object as ``(field, value)`` pairs in :data:`NOISE_FIELDS` order."""
    if not isinstance(value, Mapping) or not value:
        raise PlanError(f'{where} must be an object with level and/or sweep, e.g. {{"level": 0.8, "sweep": 0.7}}')
    unknown = [key for key in value if key not in NOISE_FIELDS]
    if unknown:
        raise PlanError(f"{where} field {unknown[0]!r} is not one of {list(NOISE_FIELDS)}")
    pairs: list[tuple[str, float]] = []
    for key in NOISE_FIELDS:
        if key not in value:
            continue
        number = _number(value[key], f"{where}.{key}")
        low = 0.0 if key == "level" else -1.0
        if not low <= number <= 1.0:
            raise PlanError(f"{where}.{key} must be within {low:g}..1, got {number:g}")
        pairs.append((key, number))
    return pairs


def _check_control(name: str, value: float, deck: int | None, where: str) -> None:
    """A control's deck scope and value range, or :class:`PlanError`."""
    if name in DECK_CONTROLS and deck is None:
        raise PlanError(f"{where}: {name} needs a deck")
    if name in MIXER_CONTROLS and deck is not None:
        raise PlanError(f"{where}: {name} is mixer-wide; deck must be null")
    if name == "tempo":
        if not 0.0 < value <= 4.0:
            raise PlanError(f"{where}: tempo is a ratio in (0, 4], got {value}")
    elif not 0.0 <= value <= 1.0:
        raise PlanError(f"{where}: {name} must be within 0..1 (bus control units), got {value}")


def _event(document: Any, index: int) -> list[PlanEvent]:
    where = f"events[{index}]"
    if not isinstance(document, Mapping):
        raise PlanError(f"{where} must be an object")
    kinds = [key for key in ("do", "set", "ramp") if key in document]
    if len(kinds) != 1:
        raise PlanError(f"{where} must carry exactly one of do, set or ramp")
    at_bar = _number(document.get("at_bar"), f"{where}.at_bar")
    raw_deck = document.get("deck")
    deck = None if raw_deck is None else _deck(raw_deck, f"{where}.deck")
    kind = kinds[0]
    if kind == "do":
        action = _member(document["do"], ACTIONS, f"{where}.do")
        if deck is None:
            raise PlanError(f"{where}: do {action} needs a deck")
        return [PlanEvent(at_bar=at_bar, deck=deck, kind="do", name=str(action))]
    name, value = _single(document[kind], f"{where}.{kind}")
    if name == "fx_noise":
        if deck is None:
            raise PlanError(f"{where}: fx_noise needs a deck")
        fields = _noise_fields(value, f"{where}.{kind}.fx_noise")
    else:
        _check_control(name, value, deck, where)
        fields = [(None, value)]
    over_bars = 0.0
    if kind == "ramp":
        over_bars = _number(document.get("over_bars", 0), f"{where}.over_bars")
        if over_bars <= 0:
            raise PlanError(f"{where}: a ramp needs over_bars > 0 (use set for a jump)")
    return [
        PlanEvent(at_bar=at_bar, deck=deck, kind=kind, name=name, value=number, over_bars=over_bars, field=part)
        for part, number in fields
    ]


def _tempo_mode(document: Mapping[str, Any]) -> str:
    """The tempo mode: synced when the field is absent or null; an object must name its mode, and anything else is refused, never defaulted."""
    if "tempo" not in document or document["tempo"] is None:
        return "sync_to_out"
    tempo = document["tempo"]
    if not isinstance(tempo, Mapping) or "mode" not in tempo:
        raise PlanError(f"tempo must be an object with a mode, got {tempo!r}")
    return _member(tempo["mode"], TEMPO_MODES, "tempo.mode")


def _optional_text(value: Any, field: str) -> str | None:
    """Text, or absent. Any other type is refused, so no metadata is silently dropped on the way through."""
    if value is None or isinstance(value, str):
        return value
    raise PlanError(f"{field} must be text")


def _bass_entry(document: Any) -> BassEntry | None:
    if document is None:
        return None
    where = "bass_entry"
    if not isinstance(document, Mapping):
        raise PlanError(f"{where} must be an object or null")
    in_bar = document.get("in_bar")
    entry = document.get("entry_plan_bar")
    note = document.get("note")
    if not isinstance(note, str) or not note:
        raise PlanError(f"{where}.note must say how the entry was found")
    if (in_bar is None) != (entry is None):
        raise PlanError(f"{where}.in_bar and entry_plan_bar are known together or not at all")
    return BassEntry(
        in_bar=None if in_bar is None else _int(in_bar, f"{where}.in_bar", minimum=1),
        entry_plan_bar=None if entry is None else _number(entry, f"{where}.entry_plan_bar"),
        hit_plan_bar=_number(document.get("hit_plan_bar"), f"{where}.hit_plan_bar"),
        note=note,
    )


def _nesting_depth(value: Any) -> int:
    """How many containers deep ``value`` goes (a scalar is 0), walked without recursion."""
    deepest, stack = 0, [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, Mapping):
            stack.extend((child, depth + 1) for child in item.values())
            deepest = max(deepest, depth + 1)
        elif isinstance(item, list | tuple):
            stack.extend((child, depth + 1) for child in item)
            deepest = max(deepest, depth + 1)
    return deepest


def _extensions(document: Mapping[str, Any], known: set[str]) -> dict[str, Any]:
    """The plan's extension keys, each refused past :data:`EXTRA_MAX_DEPTH`."""
    extra = {key: value for key, value in document.items() if key not in known}
    for key, value in extra.items():
        if _nesting_depth(value) > EXTRA_MAX_DEPTH:
            raise PlanError(f"{key} is nested too deeply: an extension may nest at most {EXTRA_MAX_DEPTH} levels")
    return extra


def parse_plan(document: Any) -> TransitionPlan:
    """A :class:`TransitionPlan` from its JSON, or :class:`PlanError` naming the field."""
    if not isinstance(document, Mapping):
        raise PlanError("a plan must be a JSON object")
    style_name = document.get("style")
    if not isinstance(style_name, str) or not style_name:
        raise PlanError("style must name a transition style", path="style")
    events = document.get("events")
    if not isinstance(events, list) or not events:
        raise PlanError("events must be a non-empty list")
    tempo_mode = _tempo_mode(document)
    anchor = _in_field("anchor", lambda: _member(document.get("anchor", "window"), ANCHORS, "anchor"))
    known = {"out", "in", "style", "events", "tempo", "anchor", "intent", "rationale", "bass_entry", "shape"}
    shape = document.get("shape")
    if shape is not None and shape not in SHAPES:
        raise PlanError(f"shape must be one of {sorted(SHAPES)} or null, got {shape!r}")
    parsed_events = [
        parsed
        for index, event in enumerate(events)
        for parsed in _in_field(f"events[{index}]", partial(_event, event, index))
    ]
    return TransitionPlan(
        out=_in_field("out", lambda: _side(document.get("out"), "out")),
        in_=_in_field("in", lambda: _side(document.get("in"), "in")),
        style=style_name,
        events=tuple(sorted(parsed_events, key=lambda e: e.at_bar)),
        tempo_mode=str(tempo_mode),
        anchor=str(anchor),
        intent=_optional_text(document.get("intent"), "intent"),
        rationale=_optional_text(document.get("rationale"), "rationale"),
        bass_entry=_bass_entry(document.get("bass_entry")),
        shape=shape,
        extra_json=json.dumps(_extensions(document, known), sort_keys=True, separators=(",", ":")),
    )


# ----- simulation ----------------------------------------------------------


@dataclass
class _DeckSim:
    fader: float = 1.0
    eq_low: float = FLAT
    playing_from: float | None = None


class ChannelStart(NamedTuple):
    """Where one channel's fader, trim and low EQ stand before the plan's first event."""

    fader: float = 1.0
    trim: float = LOAD_TRIM
    low: float = FLAT


#: Both channels at full fader, the shared load trim and flat lows: the start when the mixer is unknown.
UNTOUCHED = (ChannelStart(), ChannelStart())


@dataclass(frozen=True)
class BarState:
    """Control values at the START of one plan bar (bar 0 .. bars-1)."""

    bar: int
    out_fader: float
    out_low: float
    in_fader: float
    in_low: float
    in_playing: bool
    out_playing: bool = True
    crossfader: float = CROSSFADER_CENTER
    out_trim: float = UNITY_TRIM
    in_trim: float = UNITY_TRIM
    out_dry: float = 1.0


def _value_at(events: list[PlanEvent], deck: int | None, control: str, at: float, start: float) -> float:
    """The control's value at plan time ``at``: the latest set or ramp at or before it wins.

    A ramp moves from the value its lane holds when it starts, as the page's
    executor does: a later move cancels an earlier ramp still under way.
    """
    lane = _lane(events, deck, control)
    return _lane_value(lane, bisect_right([e.at_bar for e in lane], at), at, start)


def _lane(events: list[PlanEvent], deck: int | None, control: str) -> list[PlanEvent]:
    """The control's sets and ramps in bar order (stable, so same-bar events keep plan order).

    Field-split controls (fx_noise) are read with :func:`_field_end`, never here."""
    return sorted(
        (e for e in events if e.deck == deck and e.name == control and e.kind in ("set", "ramp")),
        key=lambda e: e.at_bar,
    )


def _lane_value(lane: list[PlanEvent], count: int, at: float, start: float) -> float:
    """``_value_at`` over a bar-ordered lane whose first ``count`` events are at or before ``at``."""
    if count == 0:
        return start
    last = lane[count - 1]
    target = last.target()
    if last.kind == "set" or at >= last.at_bar + last.over_bars:
        return target
    begin = _lane_value(lane, count - 1, last.at_bar, start)
    return begin + (target - begin) * (at - last.at_bar) / last.over_bars


class _Replay:
    """A plan's events indexed once for sampling (Codex #7595: re-sorting every lane per sample made a live proposal
    slower than its scheduling deadline). Each lane and each deck's transport steps are computed once per replay."""

    def __init__(self, events: list[PlanEvent]) -> None:
        self._events = events
        self._lanes: dict[tuple[int | None, str], tuple[list[PlanEvent], list[float]]] = {}
        self._transport: dict[int, list[tuple[float, bool]]] = {}
        #: Per lane and start value, what each event's ramp starts from, so a sample never re-walks the lane.
        self._begins: dict[tuple[int | None, str, float], list[float]] = {}

    def value(self, deck: int | None, control: str, at: float, start: float) -> float:
        key = (deck, control)
        if key not in self._lanes:
            lane = _lane(self._events, deck, control)
            self._lanes[key] = (lane, [e.at_bar for e in lane])
        lane, bars = self._lanes[key]
        count = bisect_right(bars, at)
        if count == 0:
            return start
        last = lane[count - 1]
        target = last.target()
        if last.kind == "set" or at >= last.at_bar + last.over_bars:
            return target
        begin = self._begins_of(deck, control, lane, start)[count - 1]
        return begin + (target - begin) * (at - last.at_bar) / last.over_bars

    def _begins_of(self, deck: int | None, control: str, lane: list[PlanEvent], start: float) -> list[float]:
        key = (deck, control, start)
        if key not in self._begins:
            begins: list[float] = []
            for index, event in enumerate(lane):
                previous = lane[index - 1] if index else None
                begin = start
                if previous is not None:
                    target = previous.target()
                    done = previous.kind == "set" or event.at_bar >= previous.at_bar + previous.over_bars
                    span = (event.at_bar - previous.at_bar) / previous.over_bars if not done else 1.0
                    begin = target if done else begins[-1] + (target - begins[-1]) * span
                begins.append(begin)
            self._begins[key] = begins
        return self._begins[key]

    def playing(self, deck: int, at: float, start: bool) -> bool:
        if deck not in self._transport:
            self._transport[deck] = _transport_steps(self._events, deck)
        return _playing_from(self._transport[deck], at, start)


def launch_bar(at_bar: float, previous: float | None) -> float:
    """A quantized play's dispatch bar: the lead, clamped to the executor's earliest bar.

    A play is never dispatched before the deck's previous play or stop: moving it earlier than a stop
    it follows would reorder the authored transport, and the stop would clear the armed launch.
    """
    lead = max(EARLIEST_BAR, at_bar - PLAY_LEAD_BARS)
    return lead if previous is None else max(previous, lead)


def first_dispatch_bar(plan: TransitionPlan) -> float:
    """The plan bar the page sends the plan's first command at (Sol P1 #7595): a quantized play goes out at its
    ``launch_bar``, ahead of its authored bar and never before its deck's previous transport event, as compile
    dispatches it; anything else at its bar. Validation, the proposal filter and the anchor pre-roll all read this one
    helper, so none of them measures the first command from its authored bar."""
    bars: list[float] = []
    previous: dict[int | None, float] = {}
    for event in sorted(plan.events, key=lambda e: e.at_bar):
        is_transport = event.kind == "do" and event.name in ("play", "stop")
        bars.append(
            launch_bar(event.at_bar, previous.get(event.deck))
            if is_transport and event.name == "play"
            else event.at_bar
        )
        previous.update({event.deck: event.at_bar} if is_transport else {})
    return min(bars, default=0.0)


def play_start_bar(at_bar: float, previous: float | None) -> float:
    """Where a quantized play actually starts: the master's first downbeat strictly after its dispatch.

    Public: the ONE quantized launch time. Validation (transport replay, trim order, tempo gates), compile (through
    ``launch_bar``) and the offline renderer all read it, so none of them models the launch on its own.

    The launch planner takes a beat 1 that lands outside the processor lead window, and a dispatch
    that runs on a downbeat (or on the earliest bar) has already passed that downbeat by the time it
    is planned, so the start is the following one.
    """
    return float(math.floor(launch_bar(at_bar, previous)) + 1)


def _transport_steps(events: list[PlanEvent], deck: int) -> list[tuple[float, bool]]:
    """Each play or stop of ``deck`` as ``(bar it takes effect, playing after it)``, in effect order.

    A play takes effect at its quantized start (``play_start_bar``), not at its authored bar, so a
    stop authored after a play can still take effect before it.
    """
    steps: list[tuple[float, bool]] = []
    previous: float | None = None
    armed: int | None = None  # index in steps of a play still waiting for its downbeat
    for event in sorted(
        (e for e in events if e.kind == "do" and e.deck == deck and e.name in ("play", "stop")),
        key=lambda e: e.at_bar,
    ):
        # A play or a stop before the pending launch's downbeat clears it: a second play replaces the launch, and a
        # stop clears it, so that play never starts the deck.
        if armed is not None and steps[armed][0] > event.at_bar:
            steps[armed] = (math.inf, False)
        if event.name == "play":
            effective = play_start_bar(event.at_bar, previous)
            armed = len(steps)
        else:
            effective = event.at_bar
            armed = None
        steps.append((effective, event.name == "play"))
        previous = event.at_bar
    return sorted((step for step in steps if step[0] != math.inf), key=lambda step: step[0])


def _playing_at(events: list[PlanEvent], deck: int, at: float, start: bool) -> bool:
    """Whether ``deck`` is playing at plan time ``at``: the latest play or stop to take effect by then wins."""
    return _playing_from(_transport_steps(events, deck), at, start)


def _playing_from(steps: list[tuple[float, bool]], at: float, start: bool) -> bool:
    playing = start
    for effective, is_play in steps:
        if effective > at:
            break
        playing = is_play
    return playing


def _playing_intervals(plan: TransitionPlan) -> list[tuple[float, float]]:
    """The clock-bar spans the incoming deck is playing, from each effective play to the next stop."""
    intervals: list[tuple[float, float]] = []
    since: float | None = None
    for effective, is_play in _transport_steps(plan.events, plan.in_.deck):
        if is_play and since is None:
            since = effective
        elif not is_play and since is not None:
            intervals.append((since, effective))
            since = None
    if since is not None:
        intervals.append((since, math.inf))
    return intervals


def _incoming_playing_bars(plan: TransitionPlan, begin: float, end: float) -> float:
    """Clock bars in ``[begin, end]`` the incoming deck is playing; a paused stretch is not heard."""
    return sum(max(0.0, min(stop, end) - max(start, begin)) for start, stop in _playing_intervals(plan))


def _base_rate(plan: TransitionPlan, in_profile: TrackProfile, live: Mapping[str, Any] | None) -> float:
    """Incoming grid bars per clock bar before any authored tempo.

    Synced, it is the fold Beat Sync will pick (the tempo is not set yet at validation). Unsynced, the deck keeps
    its current effective tempo, so it consumes the ratio of its tempo to the clock's.
    """
    if plan.tempo_mode == "sync_to_out":
        return _beat_sync_fold(plan, in_profile, live)
    decks = live_child((live or {}), "decks", "live mirror decks")
    if not isinstance(decks, Mapping):
        return 1.0
    out_state = live_child(decks, str(plan.out.deck), "live mirror deck")
    in_state = live_child(decks, str(plan.in_.deck), "live mirror deck")
    if not isinstance(out_state, Mapping) or not isinstance(in_state, Mapping):
        return 1.0
    out_bpm, in_bpm = out_state.get("effective_bpm"), in_state.get("effective_bpm")
    if not isinstance(out_bpm, (int, float)) or not isinstance(in_bpm, (int, float)) or out_bpm <= 0 or in_bpm <= 0:
        return 1.0
    return float(in_bpm) / float(out_bpm)


def beat_sync_reach_pct(in_profile: TrackProfile) -> float:
    """AUTOPLAY-REACH: how far Beat Sync can stretch the incoming track: its deck's real pitch range when a deck
    reports one (the engine's own bound), else the 6% default. Wider only removes a false "beyond reach"."""
    reach = in_profile.deck_pitch_range_pct
    return reach if reach is not None and reach > 0 else BEATMATCH_LIMIT_PCT


def pair_beat_sync_fold(out_bpm: float | None, in_bpm: float | None, range_pct: float = BEATMATCH_LIMIT_PCT) -> float:
    """The fold Beat Sync picks for a pair without a live mix: incoming grid bars per clock bar (1, 0.5 or 2).

    The first of the raw ratio, half time and double time inside ``range_pct`` wins, as in :func:`sync_tempo_ratio`;
    1 when none fits or a tempo is unknown.
    """
    if out_bpm is None or in_bpm is None or out_bpm <= 0 or in_bpm <= 0:
        return 1.0
    raw = out_bpm / in_bpm
    for fold in TEMPO_FOLDS:
        if abs(raw * fold - 1.0) * 100.0 <= range_pct + 1e-9:
            return fold
    return 1.0


def _beat_sync_fold(plan: TransitionPlan, in_profile: TrackProfile, live: Mapping[str, Any] | None) -> float:
    """Incoming grid bars per clock bar: 2 or 0.5 when Beat Sync folds the tempo, else 1.

    Beat Sync's applied ratio is ``raw * fold`` with ``raw = out / in``, so the incoming deck
    advances ``in * ratio / out == fold`` of its bars for each outgoing bar.
    """
    decks = live_child((live or {}), "decks", "live mirror decks")
    if plan.tempo_mode != "sync_to_out" or not isinstance(decks, Mapping):
        return 1.0
    out_state = live_child(decks, str(plan.out.deck), "live mirror deck")
    in_state = live_child(decks, str(plan.in_.deck), "live mirror deck")
    if not isinstance(out_state, Mapping) or not isinstance(in_state, Mapping):
        return 1.0
    range_pct = in_state.get("pitch_range")
    out_bpm = out_state.get("effective_bpm")
    if not isinstance(range_pct, (int, float)) or not isinstance(out_bpm, (int, float)) or out_bpm <= 0:
        return 1.0
    ratio = sync_tempo_ratio(out_state, in_profile, float(range_pct))
    if ratio is None:
        return 1.0
    return ratio * in_profile.bpm / float(out_bpm)


def _clock_pitch(plan: TransitionPlan, live: Mapping[str, Any] | None) -> float:
    """The clock deck's current pitch ratio: the base rate's effective tempo already includes it. 1.0 when unknown."""
    decks = live_child((live or {}), "decks", "live mirror decks")
    out_state = live_child(decks, str(plan.out.deck), "live mirror deck") if isinstance(decks, Mapping) else None
    pitch = out_state.get("pitch") if isinstance(out_state, Mapping) else None
    return float(pitch) if isinstance(pitch, (int, float)) and pitch > 0 else 1.0


def _incoming_pitch(plan: TransitionPlan, live: Mapping[str, Any] | None) -> float:
    """The incoming deck's current pitch ratio: where an authored tempo ramp starts, as the page's executor reads it. 1.0 when unknown."""
    decks = live_child((live or {}), "decks", "live mirror decks")
    in_state = live_child(decks, str(plan.in_.deck), "live mirror deck") if isinstance(decks, Mapping) else None
    pitch = in_state.get("pitch") if isinstance(in_state, Mapping) else None
    return float(pitch) if isinstance(pitch, (int, float)) and pitch > 0 else 1.0


def _tempo_scale(plan: TransitionPlan, in_profile: TrackProfile, live: Mapping[str, Any] | None) -> float:
    """Grid bars per clock bar for one unit of authored tempo: the track's own tempo over the clock's effective tempo.

    An authored tempo is an absolute pitch ratio (the page's ``setTempoRatio``), so it is converted by the two
    tracks' tempos, not multiplied into the base rate. 1.0 when either tempo is unknown.
    """
    decks = live_child((live or {}), "decks", "live mirror decks")
    if not isinstance(decks, Mapping) or not in_profile.bpm or in_profile.bpm <= 0:
        return 1.0
    out_state = live_child(decks, str(plan.out.deck), "live mirror deck")
    out_bpm = out_state.get("effective_bpm") if isinstance(out_state, Mapping) else None
    if not isinstance(out_bpm, (int, float)) or out_bpm <= 0:
        return 1.0
    return in_profile.bpm / float(out_bpm)


def _authored_tempos(plan: TransitionPlan, deck: int | None) -> list[float]:
    return [
        float(e.value)
        for e in plan.events
        if e.kind != "do" and e.deck == deck and e.name == "tempo" and e.value is not None
    ]


def _incoming_rate(plan: TransitionPlan, base: float, scale: float = 1.0, clock_pitch: float = 1.0) -> float:
    """The grid rate the incoming deck is bounded at, per clock bar.

    The fastest of the base rate and the authored incoming tempos converted by ``scale``, over the slowest clock
    tempo: a clock slowed by its own tempo move stretches each clock bar, so the incoming deck spans more grid bars
    in it. Authored clock tempos are absolute ratios, so they are taken against the clock's current ``clock_pitch``,
    which the base rate already includes. The curve of a ramp is not modeled, so the bound takes the extremes the deck
    reaches. An over-estimate
    refuses a plan that might run out of grid; it never passes one that might.
    """
    fastest = max([base, *(tempo * scale for tempo in _authored_tempos(plan, plan.in_.deck))])
    return fastest * clock_pitch / min([clock_pitch, *_authored_tempos(plan, plan.out.deck)])


def _terminal_handover(plan: TransitionPlan) -> tuple[float, int] | None:
    """(bar, plan index) of the plan's LAST ``master`` handover to the incoming deck, the one that leaves it leading
    (Sol P1 #7594: an earlier one may be handed back), or None when it has none. Same-bar events keep plan order."""
    return max(
        (
            (e.at_bar, i)
            for i, e in enumerate(plan.events)
            if e.kind == "do" and e.name == "master" and e.deck == plan.in_.deck
        ),
        default=None,
    )


def handover_bar(plan: TransitionPlan) -> float | None:
    """The bar of the plan's terminal ``master`` handover to the incoming deck, or None when it has none."""
    terminal = _terminal_handover(plan)
    return None if terminal is None else terminal[0]


def prelaunch_synced_tempo_moves(plan: TransitionPlan) -> list[PlanEvent]:
    """A synced plan's incoming tempo moves that run before the incoming deck actually starts (Codex + Sol #7594).

    The start is the quantized launch's own downbeat (``_incoming_start``, from ``play_start_bar``, the function compile
    times the launch with), not the authored play bar: the play is dispatched early and the deck starts later. A move
    before that start goes to the leader while the follower is still paused, and a quantized launch does not retime
    the follower, so it would start at the old tempo against a moved leader and drift. Such a plan is refused."""
    start = _incoming_start(plan) if plan.tempo_mode == "sync_to_out" else None
    if start is None:
        return []
    return [e for e in plan.events if e.deck == plan.in_.deck and e.name == "tempo" and e.at_bar < start]


def incoming_leads(plan: TransitionPlan, index: int) -> bool:
    """Whether event ``index`` runs once the incoming deck is master for good: after the terminal handover in (bar,
    plan order).

    An event at the handover's own bar but authored before the ``master`` event still runs while the incoming deck is a
    synced follower (Codex #7594), so bar alone does not decide it."""
    terminal = _terminal_handover(plan)
    return terminal is not None and (plan.events[index].at_bar, index) > terminal


def _synced_grid_view(plan: TransitionPlan) -> TransitionPlan:
    """The plan as the incoming GRID sees it (AGENT-32, Codex #7594).

    A synced plan's incoming tempo moves before the handover run on the leader (compile's ``_leader_led_events``),
    and the follower keeps its bar ratio to the clock, so they do not change how many incoming bars a clock bar
    holds: they are dropped here. Moves from the handover on stay, since the incoming deck leads by then."""
    if plan.tempo_mode != "sync_to_out":
        return plan
    kept = tuple(
        e
        for i, e in enumerate(plan.events)
        if not (e.deck == plan.in_.deck and e.name == "tempo" and not incoming_leads(plan, i))
    )
    return replace(plan, events=kept)


def _incoming_grid_bars(plan: TransitionPlan, base: float, scale: float = 1.0, clock_pitch: float = 1.0) -> float:
    """Incoming grid bars the plan consumes: the playing span from any pre-roll to the end, at the bounded rate."""
    rate = _incoming_rate(plan, base, scale, clock_pitch)
    authored = _incoming_play_authored(plan)
    begin = min(0.0, authored) if authored is not None else min(0.0, _incoming_start(plan) or 0.0)
    return rate * _incoming_playing_bars(plan, begin, plan.bars)


def simulate(
    plan: TransitionPlan,
    *,
    crossfader: float = CROSSFADER_CENTER,
    channels: tuple[ChannelStart, ChannelStart] = UNTOUCHED,
) -> list[BarState]:
    """Replay the plan's mixer moves and transport bar by bar (sampled at each bar's middle).

    The outgoing deck starts playing (validation requires it) and the incoming deck starts
    cued; every ``play`` and ``stop`` on either deck is replayed, so a plan that stops a
    deck mid-transition shows that deck as unheard from then on. The crossfader starts at
    ``crossfader`` (the live mixer's, when known) and follows the plan's own moves; the
    outgoing and incoming faders, trims and lows start at ``channels`` the same way.
    """
    events = _replay_events(plan, channels)
    return [_state_of(plan, events, bar + 0.5, bar, crossfader, channels) for bar in range(plan.bars)]


def cap_outgoing_fader(plan: TransitionPlan, ceiling: float) -> TransitionPlan:
    """AGENT-35: a plan never makes the outgoing deck louder than the DJ left it. Every fader target the plan sets on
    the outgoing deck is ``min(planned, ceiling)``, the live fader; 1.0 (no live fader known) leaves the plan as written.
    The incoming deck is the one coming up, so its levels stand."""
    outgoing = [e for e in plan.events if e.deck == plan.out.deck and e.name == "fader" and e.kind in ("set", "ramp")]
    # Every outgoing fader target is a level between 0 and 1 whatever the ceiling (Sol P1 #7595: the unity fast path
    # used to skip this), so an out-of-range target is refused, never emitted.
    for event in outgoing:
        _unit_level(event.target(), f"outgoing fader at bar {event.at_bar:g}")
    if ceiling >= 1.0:
        return plan
    capped = tuple(replace(e, value=min(e.target(), ceiling)) if e in outgoing else e for e in plan.events)
    return replace(plan, events=capped)


def live_outgoing_fader(plan: TransitionPlan, live: Mapping[str, Any] | None) -> float | None:
    """The outgoing channel's live fader from the mirror, or None when the mirror does not carry it (absent at any
    level). A value that IS there but is not a unit number raises: a corrupt mirror is never read as "no fader"."""
    channel = _live_channel(live, plan.out.deck)
    if channel is None or "fader" not in channel:
        return None
    return _unit_level(channel["fader"], f"deck {plan.out.deck} live fader")


def _live_channel(live: Mapping[str, Any] | None, deck: int) -> Mapping[str, Any] | None:
    """The mirror's channel for a deck, None when any level is absent; a level present with the wrong type raises."""
    level: Mapping[str, Any] = live or {}
    for key, where in (
        ("mixer", "live mirror mixer"),
        ("channels", "live mirror channels"),
        (str(deck), "live mirror channel"),
    ):
        if key not in level:
            return None
        level = live_mapping(level[key], where)
    return level


def _unit_level(value: Any, where: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0.0 <= value <= 1.0
    ):
        raise ValueError(f"{where} {value!r} is not a level between 0 and 1")
    return float(value)


def state_at(
    plan: TransitionPlan,
    at: float,
    *,
    bar: int,
    crossfader: float = CROSSFADER_CENTER,
    channels: tuple[ChannelStart, ChannelStart] = UNTOUCHED,
) -> BarState:
    """The mixer and transport at plan time ``at``, every event at or before it applied.

    The outgoing fader is capped at where its channel starts (``cap_outgoing_fader``), as compile caps it live."""
    return _state_of(plan, _replay_events(plan, channels), at, bar, crossfader, channels)


def _replay_events(plan: TransitionPlan, channels: tuple[ChannelStart, ChannelStart]) -> _Replay:
    """The events a replay samples: the outgoing fader capped once per replay, not per sample (Codex #7595), and each
    lane indexed once."""
    return _Replay(list(cap_outgoing_fader(plan, channels[0].fader).events))


def _state_of(
    plan: TransitionPlan,
    events: _Replay,
    at: float,
    bar: int,
    crossfader: float,
    channels: tuple[ChannelStart, ChannelStart],
) -> BarState:
    out_start, in_start = channels
    return BarState(
        bar=bar,
        out_fader=events.value(plan.out.deck, "fader", at, out_start.fader),
        out_low=events.value(plan.out.deck, "eq_low", at, out_start.low),
        in_fader=events.value(plan.in_.deck, "fader", at, in_start.fader),
        in_low=events.value(plan.in_.deck, "eq_low", at, in_start.low),
        in_playing=events.playing(plan.in_.deck, at, False),
        out_playing=events.playing(plan.out.deck, at, True),
        crossfader=events.value(None, "crossfader", at, crossfader),
        out_trim=events.value(plan.out.deck, "trim", at, out_start.trim),
        in_trim=events.value(plan.in_.deck, "trim", at, in_start.trim),
        out_dry=events.value(plan.out.deck, "fx_dry", at, 1.0),
    )


# ----- validation ----------------------------------------------------------


@dataclass
class Validation:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "metrics": dict(self.metrics),
        }


def bpm_gap_pct(out_bpm: float | None, in_bpm: float | None) -> float | None:
    """Tempo gap in percent, folding half and double time."""
    if not out_bpm or not in_bpm or out_bpm <= 0 or in_bpm <= 0:
        return None
    ratio = max(out_bpm, in_bpm) / min(out_bpm, in_bpm)
    if abs(ratio / 2.0 - 1.0) < abs(ratio - 1.0):
        ratio /= 2.0
    return abs(ratio - 1.0) * 100.0


_BAR_SAMPLES = 32


def _in_bar(
    plan: TransitionPlan,
    plan_bar: int,
    base: float,
    scale: float = 1.0,
    clock_pitch: float = 1.0,
    in_pitch: float = 1.0,
) -> int:
    """The incoming grid bar sounding at ``plan_bar``.

    Each sample of the bars before it that the incoming deck played advances the grid at the rate it runs at then:
    the base rate until an authored tempo is set, and that tempo converted by ``scale`` after it, so an authored
    tempo heard before the bar counts and one after it does not. The clock deck's own tempo at that sample stretches
    the clock bar, so the grid advances by the rate divided by it.
    """
    return _in_bars(plan, plan_bar, base, scale, clock_pitch, in_pitch)[plan_bar]


def _in_bars(
    plan: TransitionPlan, last_bar: int, base: float, scale: float, clock_pitch: float, in_pitch: float
) -> list[int]:
    """:func:`_in_bar` for every plan bar 0..``last_bar``, in one pass over the samples."""
    replay = _Replay(plan.events)
    tempo_bars = [
        e.at_bar for e in plan.events if e.kind in ("set", "ramp") and e.deck == plan.in_.deck and e.name == "tempo"
    ]
    first_tempo = min(tempo_bars, default=math.inf)
    shift = _launch_grid_shift(plan)
    consumed = 0.0
    bars = [plan.in_.start_bar + shift]
    for sample in range(last_bar * _BAR_SAMPLES):
        at = (sample + 0.5) / _BAR_SAMPLES
        if replay.playing(plan.in_.deck, at, False):
            rate = replay.value(plan.in_.deck, "tempo", at, in_pitch) * scale if first_tempo <= at else base
            clock = replay.value(plan.out.deck, "tempo", at, clock_pitch) / clock_pitch
            consumed += rate / clock / _BAR_SAMPLES
        if (sample + 1) % _BAR_SAMPLES == 0:
            bars.append(plan.in_.start_bar + shift + int(consumed))
    return bars


def validate_plan(
    plan: TransitionPlan,
    out_profile: TrackProfile,
    in_profile: TrackProfile,
    *,
    live: Mapping[str, Any] | None = None,
) -> Validation:
    """Errors (refuse to schedule), warnings (allowed, said) and clash metrics."""
    result = Validation()
    base = _base_rate(plan, in_profile, live)
    scale = _tempo_scale(plan, in_profile, live)
    clock_pitch = _clock_pitch(plan, live)
    in_pitch = _incoming_pitch(plan, live)
    _check_structure(plan, out_profile, in_profile, result, base, scale, clock_pitch)
    _check_trim(plan, _channel_start(live_child((live or {}), "mixer"), plan.in_.deck), result)
    _check_fit(plan, out_profile, in_profile, result)
    _check_clashes(
        plan, out_profile, in_profile, result, live_child((live or {}), "mixer"), base, scale, (clock_pitch, in_pitch)
    )
    if live is not None:
        _validate_live(plan, (out_profile, in_profile), live, result)
    return result


def _check_trim(plan: TransitionPlan, start: ChannelStart, result: Validation) -> None:
    """AGENT-31: the incoming trim. ``in.trim_db`` must be a gain the TRIM knob reaches, it is the only incoming trim
    (compile hoists it into prepare, so an authored incoming trim would fight it), and any authored incoming trim move
    lands before the incoming fader first opens: a trim that moves while the deck is heard is an audible gain jump."""
    trim_db = plan.in_.trim_db
    if trim_db is not None and trim_db > TRIM_MAX_DB + 1e-9:
        result.errors.append(f"in.trim_db {trim_db:+.2f} dB is past the trim knob's {TRIM_MAX_DB:+.2f} dB")
    trims = [e for e in plan.events if e.deck == plan.in_.deck and e.name == "trim"]
    if trim_db is not None and trims:
        result.errors.append("in.trim_db and an authored incoming trim both set the incoming trim: keep one")
    _check_trim_order(plan, start, result)


def hoistable(event: PlanEvent, plan: TransitionPlan) -> bool:
    """Whether an incoming-deck set at bar 0 or before runs in ``prepare`` (compile hoists it there).

    A prepare command runs before every timed event, so it may not jump a
    ramp or set on its control that validation orders first. Only a set with
    nothing else on its control at or before its bar is hoisted.
    """
    if event.kind != "set" or event.deck != plan.in_.deck or event.at_bar > 0:
        return False
    return not any(
        other is not event and other.deck == event.deck and other.name == event.name and other.at_bar <= event.at_bar
        for other in plan.events
    )


def _page_order(plan: TransitionPlan, deck: int) -> list[PlanEvent]:
    """A deck's events in the order the page runs them: the sets compile hoists into prepare (only sets, no transport
    or ramp), then the timed events by dispatch bar, a quantized play at its ``launch_bar`` (never before the deck's
    last transport event, as compile dispatches it) and everything else at its bar, authored order within a bar."""
    events = [e for e in plan.events if e.deck == deck]
    dispatch: dict[int, float] = {}
    previous: float | None = None
    for event in (e for e in events if e.kind == "do" and e.name in ("play", "stop")):
        dispatch[id(event)] = launch_bar(event.at_bar, previous) if event.name == "play" else event.at_bar
        previous = event.at_bar
    timed = sorted((e for e in events if not hoistable(e, plan)), key=lambda e: dispatch.get(id(e), e.at_bar))
    return [*(e for e in events if hoistable(e, plan)), *timed]


def _check_trim_order(plan: TransitionPlan, start: ChannelStart, result: Validation) -> None:
    """One walk over the incoming deck's events in the order the page runs them: the sets compile hoists into prepare,
    then the timed events by dispatch bar (a quantized play at its ``launch_bar``, everything else at its bar), authored
    order within a bar. The deck is cued until the play's downbeat (``play_start_bar``) and quiet again from a stop,
    which also clears a launch still waiting; a closing fader ramp counts as open until its end. A trim that changes
    while the deck is audible with its fader open is a gain jump the room hears: a set when it lands, a ramp if the deck
    is audible with the fader open at any point of its span (a trim raised from 0 opens the channel and is allowed).
    Sol P1 #7594, three rounds: this one walk replaces the separate replay, transport query and ramp special cases."""
    deck = plan.in_.deck
    fader, trim = start.fader, start.trim
    closing: tuple[float, float] | None = None  # (end bar, target) of a fader ramp toward closed
    audible, launch, previous = False, None, None  # launch: the downbeat a pending quantized play starts on
    moves: list[tuple[float, float]] = []  # (start, end) of trim moves from an open trim, still to be judged

    def settle(now: float) -> None:
        """A closing fader ramp that has ended by ``now`` leaves the fader at its target."""
        nonlocal fader, closing
        if closing is not None and now >= closing[0]:
            fader, closing = closing[1], None

    def judge(now: float) -> None:
        """Every trim move running at ``now`` is heard if the deck is audible with its fader open then; a set is judged
        at its own bar only, so it leaves the list here whatever the verdict."""
        nonlocal moves
        settle(now)
        heard = (
            [(at, end) for at, end in moves if at <= now < end or at == end == now] if audible and fader > 0.0 else []
        )
        result.errors.extend(
            f"incoming trim at bar {at:g} lands while deck {deck} is heard: set the trim before its fader comes up or "
            "before it plays"
            for at, _end in heard
        )
        moves = [move for move in moves if move not in heard and move[1] > move[0]]  # a set (zero width) leaves at once

    for event in _page_order(plan, deck):  # prepare holds only hoisted sets: no play, no ramp is pending while it runs
        if launch is not None and event.at_bar >= launch:
            audible, at_launch, launch = True, launch, None
            judge(at_launch)
        settle(event.at_bar)
        if event.kind == "do" and event.name in ("play", "stop"):
            launch = play_start_bar(event.at_bar, previous) if event.name == "play" else None
            audible = audible and event.name == "play"
            previous = event.at_bar
        elif event.name == "fader":
            target = event.target()
            closing = (event.at_bar + event.over_bars, target) if event.kind == "ramp" and target < fader else None
            fader = fader if closing is not None else target
            judge(event.at_bar)
        elif event.name == "trim":
            if trim > 0.0:  # a set is a zero-width move: judged at its own bar only
                moves.append((event.at_bar, event.at_bar + (event.over_bars if event.kind == "ramp" else 0.0)))
            judge(event.at_bar)
            trim = event.target()
    if launch is not None:  # a downbeat after the deck's last event still starts it inside a ramp still running
        audible = True
        judge(launch)


def _incoming_channel_start(plan: TransitionPlan, mixer: Mapping[str, Any]) -> ChannelStart:
    """The incoming channel as prepare leaves it: ``in.trim_db`` is set there, before any timed event (AGENT-31)."""
    start = _channel_start(mixer, plan.in_.deck)
    if plan.in_.trim_db is None or plan.in_.trim_db > TRIM_MAX_DB + 1e-9:
        return start
    return start._replace(trim=trim_knob(plan.in_.trim_db))


def _incoming_play_authored(plan: TransitionPlan) -> float | None:
    """The authored bar of the incoming deck's first play, when the plan has one."""
    plays = [
        e.at_bar for e in plan.events if e.kind == "do" and e.name == "play" and e.deck == plan.in_.deck
    ]
    return min(plays) if plays else None


def _launch_grid_shift(plan: TransitionPlan) -> int:
    """Extra incoming grid bars credited from a negative authored launch lead (PR #5822 follow-up)."""
    authored = _incoming_play_authored(plan)
    if authored is None or authored >= 0:
        return 0
    return -round(authored)


def _incoming_start(plan: TransitionPlan) -> float | None:
    plays = [e for e in plan.events if e.kind == "do" and e.name == "play" and e.deck == plan.in_.deck]
    return play_start_bar(plays[0].at_bar, None) if plays else None


def _check_structure(
    plan: TransitionPlan,
    out_profile: TrackProfile,
    in_profile: TrackProfile,
    result: Validation,
    base: float,
    scale: float,
    clock_pitch: float,
) -> None:
    """Who, which style, and whether every event and both windows fit the grids."""
    errors, warnings = result.errors, result.warnings
    if plan.out.deck == plan.in_.deck:
        errors.append("out and in name the same deck")
    if plan.out.track != out_profile.stable_id:
        errors.append(f"out.track {plan.out.track} does not match the profile {out_profile.stable_id}")
    if plan.in_.track != in_profile.stable_id:
        errors.append(f"in.track {plan.in_.track} does not match the profile {in_profile.stable_id}")
    style_doc = STYLES.get(plan.style)
    if style_doc is None:
        warnings.append(f"style {plan.style!r} is not in the catalog; its events are taken as written")
    elif not style_doc.available:
        errors.append(f"style {plan.style!r} is not available: {style_doc.unavailable_reason}")
    out_end = plan.out.start_bar + plan.out.bars
    if out_end > out_profile.bars:
        errors.append(
            f"the out window ends at bar {out_end}, after the outgoing track's last downbeat "
            f"(bar {out_profile.bars}); the deck would run out before the handover"
        )
    for event in plan.events:
        errors.extend(_event_errors(plan, event))
    for event in prelaunch_synced_tempo_moves(plan):
        errors.append(
            f"incoming tempo at bar {event.at_bar:g} runs before the incoming launch in a synced plan: the follower would "
            "start at the old tempo against a moved leader; move it to or after the launch"
        )
    if _incoming_start(plan) is None:
        errors.append(f"no play event starts the incoming deck {plan.in_.deck}")
    errors.extend(_shape_errors(plan))
    errors.extend(_fx_errors(plan))
    errors.extend(_gives_way_before_hit(plan))
    # A quantized play starts on a downbeat strictly after its dispatch, so no launch plays a pre-roll.
    in_bars_heard = _incoming_grid_bars(_synced_grid_view(plan), base, scale, clock_pitch)
    if plan.in_.start_bar + in_bars_heard > in_profile.bars + 1:
        errors.append(
            f"the incoming track runs out of grid: it starts at bar {plan.in_.start_bar} and the "
            f"plan needs {in_bars_heard:g} bars, but its grid ends at bar {in_profile.bars}"
        )


def _shape_errors(plan: TransitionPlan) -> list[str]:
    """D13: a shape and the style that builds it come together, so a plan never implies an intended dip."""
    wanted = next((shape for shape, style in SHAPE_STYLES.items() if style == plan.style), None)
    if plan.shape != wanted:
        return [f"style {plan.style!r} carries shape {wanted!r}, but the plan says {plan.shape!r}"]
    return []


def _fx_errors(plan: TransitionPlan) -> list[str]:
    """Spec 09-transitions-fx 4.2: transition FX reset and tail before handover."""
    errors: list[str] = []
    out = plan.out.deck
    uses_fx = any(e.name in FX_CONTROLS for e in plan.events)
    if not uses_fx:
        return errors
    if not any(e.kind == "do" and e.name == "fx_reset" and e.deck == out and e.at_bar == plan.bars for e in plan.events):
        errors.append(
            f"the plan sets {', '.join(sorted({e.name for e in plan.events if e.name in FX_CONTROLS}))} but never "
            f"runs fx_reset at the handover (bar {plan.bars}): the deck would keep its effect"
        )
    level = _field_end(plan, out, "fx_noise", "level")
    if level is not None and level > 1e-9:
        errors.append(
            f"fx_noise level is {level:g} at the plan's end (bar {plan.bars}): cut the noise to 0 by the handover, "
            "or the next track starts under it"
        )
    sweep = _field_end(plan, out, "fx_noise", "sweep")
    if sweep is not None and abs(sweep) > 1e-9:
        errors.append(
            f"fx_noise sweep is {sweep:g} at the plan's end (bar {plan.bars}): bring the sweep back to 0 by the handover"
        )
    dry_close_end = 0.0
    for event in plan.events:
        if event.deck == out and event.name == "fx_dry" and event.kind in ("set", "ramp"):
            try:
                target = 0.0 if event.kind == "set" else event.target()
            except ValueError:
                continue
            if target < 1e-9:
                end = event.at_bar + (event.over_bars if event.kind == "ramp" else 0.0)
                dry_close_end = max(dry_close_end, end)
    if dry_close_end > plan.bars - 1 + 1e-9:
        errors.append(
            f"the dry closes at bar {dry_close_end:g}, under 1 bar before the handover at bar {plan.bars}: "
            "fx_reset would cut the echo tail"
        )
    return errors


def _field_end(plan: TransitionPlan, deck: int, name: str, field: str) -> float | None:
    """A field-split control's value at the plan's end: the latest-starting set or ramp wins, at its target."""
    lane = sorted(
        (e for e in plan.events if e.deck == deck and e.name == name and e.field == field and e.kind != "do"),
        key=lambda e: e.at_bar,
    )
    return lane[-1].value if lane else None


def _gives_way(event: PlanEvent, out_deck: int) -> bool:
    """Whether an event takes something away from the outgoing deck: a fader or EQ below full, a filter off flat, a
    stop."""
    if event.deck != out_deck:
        return False
    if event.kind == "do":
        return event.name == "stop"
    if event.value is None:
        return False
    if event.name == "fader":
        return event.value < 1.0
    if event.name in ("eq_low", "eq_mid", "eq_high"):
        return event.value < FLAT
    if event.name == "filter":
        return event.value != FLAT
    if event.name == "fx_dry" and event.value is not None:
        return event.value < 1.0
    return False


def _gives_way_before_hit(plan: TransitionPlan) -> list[str]:
    """D11: nothing on the outgoing deck before the incoming bass has landed, in every style."""
    if plan.bass_entry is None:
        return []
    hit = plan.bass_entry.hit_plan_bar
    return [
        f"the outgoing deck {plan.out.deck} gives way ({event.kind} {event.name}) at bar {event.at_bar:g}, before the"
        f" incoming bass lands at bar {hit:g} (D11)"
        for event in plan.events
        if event.at_bar < hit - 1e-9 and _gives_way(event, plan.out.deck)
    ]


def _event_errors(plan: TransitionPlan, event: PlanEvent) -> list[str]:
    errors: list[str] = []
    if event.at_bar < EARLIEST_BAR or event.at_bar > plan.bars:
        errors.append(f"event at bar {event.at_bar} is outside the plan (bars {EARLIEST_BAR}..{plan.bars})")
    if event.kind == "ramp" and event.at_bar + event.over_bars > plan.bars + 1e-9:
        errors.append(
            f"ramp of {event.name} on deck {event.deck} ends at bar "
            f"{event.at_bar + event.over_bars}, after the plan ends at {plan.bars}"
        )
    if event.deck is not None and event.deck not in (plan.out.deck, plan.in_.deck):
        errors.append(f"event at bar {event.at_bar} touches deck {event.deck}, which the plan does not name")
    if event.name in FX_CONTROLS | FX_ACTIONS and event.deck != plan.out.deck:
        errors.append(
            f"{event.name} at bar {event.at_bar:g} is on deck {event.deck}: "
            f"transition FX move only the outgoing deck {plan.out.deck}"
        )
    if event.kind == "do":
        errors.extend(_handover_errors(plan, event))
    return errors


def _handover_errors(plan: TransitionPlan, event: PlanEvent) -> list[str]:
    """Master and stop moves only at the terminal handover, the plan's last bar."""
    errors: list[str] = []
    # The incoming deck becomes master only at the terminal handover: a master move before the plan ends would reach the
    # page while the incoming transport is still paused and the outgoing deck is active, failing the plan after prepare.
    if event.name == "master" and event.deck == plan.in_.deck and event.at_bar < plan.bars:
        errors.append(
            f"incoming deck {plan.in_.deck} becomes master at bar {event.at_bar:g}, before the plan ends at {plan.bars:g}; "
            "master moves only at the terminal handover"
        )
    # The clock stops only at the handover, the plan's last bar; a stop before it would freeze the clock mid-plan.
    if event.name == "stop" and event.deck == plan.out.deck and event.at_bar < plan.bars:
        errors.append(
            f"outgoing deck {plan.out.deck} stops at bar {event.at_bar:g}, before the plan ends at {plan.bars:g}; "
            "the clock must run to the handover"
        )
    return errors


def _check_fit(plan: TransitionPlan, out_profile: TrackProfile, in_profile: TrackProfile, result: Validation) -> None:
    """Tempo, grid trust and key: allowed, but said."""
    _check_tempo_fit(plan, out_profile, in_profile, result)
    if not out_profile.grid_trusted or not in_profile.grid_trusted:
        result.warnings.append("a beatgrid is untrusted; bar positions may be off")
    _check_key_fit(plan, out_profile, in_profile, result)


def _check_tempo_fit(
    plan: TransitionPlan, out_profile: TrackProfile, in_profile: TrackProfile, result: Validation
) -> None:
    gap = bpm_gap_pct(out_profile.judged_bpm, in_profile.judged_bpm)
    result.metrics["bpm_gap_pct"] = None if gap is None else round(gap, 2)
    beatmatched = _beatmatched(plan)
    reach = beat_sync_reach_pct(in_profile)
    if gap is not None and gap > reach and beatmatched:
        result.warnings.append(
            f"tempo gap {gap:.1f}% is beyond Beat Sync's {reach:.0f}% reach; the "
            "blend will drift. A cut or drop_swap does not need beatmatching"
        )
    if plan.tempo_mode == "none" and beatmatched:
        result.warnings.append("tempo mode none on a beatmatched style: the decks will drift")


def _relation_label(relation: str) -> str:
    return relation.replace("_", "-")


def _key_fit_warning(out_key: str, in_key: str, relation: str, move: str, interval: str, *, bars: float) -> str | None:
    if bars <= 2:
        return None
    if relation == "wrong_way_diagonal":
        return f"keys {out_key} and {in_key}: wrong-way diagonal, no shared chord tones"
    from apps.shared.harmonic import CLASH_RELATION, LIFT, LONG_BLEND_KEY_CLASH_RELATIONS

    if relation in LONG_BLEND_KEY_CLASH_RELATIONS:
        tail = interval if relation != CLASH_RELATION else "no Camelot relation"
        return f"keys {out_key} and {in_key}: {_relation_label(relation)}, {tail}"
    if move == LIFT:
        return f"keys {out_key} and {in_key}: {_relation_label(relation)} ({interval}); mix it fast"
    return None


def _check_key_fit(
    plan: TransitionPlan, out_profile: TrackProfile, in_profile: TrackProfile, result: Validation
) -> None:
    if not (out_profile.key and in_profile.key):
        return
    from apps.shared.harmonic import CamelotKey, camelot_distance, key_compat

    distance = camelot_distance(CamelotKey.from_string(out_profile.key), CamelotKey.from_string(in_profile.key))
    compat = key_compat(out_profile.key, in_profile.key)
    result.metrics["key_distance"] = distance
    result.metrics["key_relation"] = compat.relation
    result.metrics["key_move"] = compat.move
    warning = _key_fit_warning(
        out_profile.key, in_profile.key, compat.relation, compat.move, compat.interval, bars=plan.bars
    )
    if warning:
        result.warnings.append(warning)


def _bar_value(series: tuple[float, ...] | None, bar: int) -> float | None:
    if series is None or not 0 < bar <= len(series):
        return None
    return series[bar - 1]


def _overlaps(
    series_out: tuple[float, ...] | None, series_in: tuple[float, ...] | None, out_bar: int, in_bar: int, floor: float
) -> bool:
    """Both decks carry this part (vocals, bass) at or above ``floor`` in their bars."""
    out_value = _bar_value(series_out, out_bar)
    in_value = _bar_value(series_in, in_bar)
    return out_value is not None and in_value is not None and min(out_value, in_value) >= floor


def _side_gain(assign: Any, crossfader: float) -> float:
    """A channel's crossfader gain on the engine's equal-power curve (``_xfGainFor``): 1.0 for THRU or unknown."""
    if assign == "A":
        return math.cos(crossfader * math.pi / 2)
    if assign == "B":
        return math.cos((1.0 - crossfader) * math.pi / 2)
    return 1.0


def _side_open(assign: Any, crossfader: float) -> bool:
    """Whether a channel on crossfader side ``assign`` passes at this crossfader position.

    A channel on side A is cut from ``CROSSFADER_CLOSED`` toward B, one on side B from
    ``1 - CROSSFADER_CLOSED`` toward A; a THRU or unknown assignment always passes.
    """
    if assign == "A":
        return crossfader < CROSSFADER_CLOSED
    if assign == "B":
        return crossfader > 1.0 - CROSSFADER_CLOSED
    return True


def _assign_of(mixer: Mapping[str, Any], deck: int) -> Any:
    return live_child(live_child(mixer, "channels", "live mirror channels"), str(deck), "live mirror channel").get(
        "assign"
    )


def read_live_channels(plan: TransitionPlan, live: Mapping[str, Any] | None) -> tuple[ChannelStart, ChannelStart]:
    """AGENT-35: both plan channels as the live mirror has them, read through the one strict reader validation uses
    (fader, trim and low EQ; a malformed present value raises, a missing one keeps its default)."""
    mixer = live_child((live or {}), "mixer", "live mirror mixer")
    return _channel_start(mixer, plan.out.deck), _channel_start(mixer, plan.in_.deck)


def _channel_start(mixer: Mapping[str, Any], deck: int) -> ChannelStart:
    """The live channel's fader, trim and low EQ; a value the mirror lacks keeps its default."""
    channel = live_child(live_child(mixer, "channels", "live mirror channels"), str(deck), "live mirror channel")
    default = ChannelStart()
    return ChannelStart(
        fader=_level_or(channel, "fader", default.fader, deck),
        trim=_level_or(channel, "trim", default.trim, deck),
        low=_level_or(channel, "eq_low", default.low, deck),
    )


def _level_or(channel: Mapping[str, Any], key: str, default: float, deck: int) -> float:
    """A live channel level: the default only when the mirror does not carry it; a malformed one raises."""
    return default if key not in channel else _unit_level(channel[key], f"deck {deck} live {key}")


def _number_or(value: Any, default: float) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def _heard(row: BarState, out_assign: Any = None, in_assign: Any = None) -> tuple[bool, bool]:
    """Whether the outgoing and the incoming deck are audible in this bar.

    A channel's level is its fader scaled by its trim (unity at ``UNITY_TRIM``), as the
    engine's input gain and fader gain multiply.
    """
    # ADR-0135: audibility is judged against the level decks load at.
    floor = AUDIBLE_FADER * LOAD_GAIN
    out_level = row.out_fader * row.out_trim / UNITY_TRIM * row.out_dry
    in_level = row.in_fader * row.in_trim / UNITY_TRIM
    return (
        row.out_playing and out_level >= floor and _side_open(out_assign, row.crossfader),
        row.in_playing and in_level >= floor and _side_open(in_assign, row.crossfader),
    )


def _check_clashes(
    plan: TransitionPlan,
    out_profile: TrackProfile,
    in_profile: TrackProfile,
    result: Validation,
    mixer: Mapping[str, Any],
    base: float,
    scale: float,
    pitches: tuple[float, float] = (1.0, 1.0),
) -> None:
    """Replay the plan bar by bar: silence, vocals over vocals, bass over bass.

    With the live mixer known, the crossfader and both channels' faders, trims and lows
    start where they are and each
    deck is heard only while its channel's side is open; without it, both channels count as
    THRU at the shared load trim.
    """
    vocal_overlap = bass_overlap = 0
    live_crossfader = mixer.get("crossfader")
    start = float(live_crossfader) if isinstance(live_crossfader, (int, float)) else CROSSFADER_CENTER
    out_assign, in_assign = _assign_of(mixer, plan.out.deck), _assign_of(mixer, plan.in_.deck)
    channels = (_channel_start(mixer, plan.out.deck), _incoming_channel_start(plan, mixer))
    silent_bars = len(_silent_bars(plan, (start, channels), (out_assign, in_assign)))
    grid_view = _synced_grid_view(plan)
    rows = simulate(plan, crossfader=start, channels=channels)
    in_bars = _in_bars(grid_view, max((row.bar for row in rows), default=0), base, scale, pitches[0], pitches[1])
    for row in rows:
        out_bar = plan.out.start_bar + row.bar
        in_bar = in_bars[row.bar]
        out_heard, in_heard = _heard(row, out_assign, in_assign)
        if not (out_heard and in_heard):
            continue
        if _overlaps(out_profile.bar_vocals, in_profile.bar_vocals, out_bar, in_bar, VOCAL_BAR_THRESHOLD):
            vocal_overlap += 1
        lows_full = row.out_low >= NEAR_FULL_LOW and row.in_low >= NEAR_FULL_LOW
        if lows_full and _overlaps(out_profile.bar_bass, in_profile.bar_bass, out_bar, in_bar, STRONG_BASS):
            bass_overlap += 1
    _report_clashes(out_profile, in_profile, (vocal_overlap, bass_overlap, silent_bars), result)
    _check_end(plan, (start, channels), (out_assign, in_assign), result)


#: What ``_heard`` reads: transport, fader, trim and crossfader. Every other event is left out of the silence replay.
_AUDIBILITY_EVENTS = frozenset({"play", "stop", "fader", "trim", "crossfader"})


def _silence_cuts(plan: TransitionPlan) -> set[float]:
    """The sample points of the silence replay: the bar grid, every event's bar, and each ramp's span."""
    cuts = {bar + step / SILENCE_SAMPLES_PER_BAR for bar in range(plan.bars) for step in range(SILENCE_SAMPLES_PER_BAR)}
    cuts.add(float(plan.bars))
    for event in plan.events:
        if event.name not in _AUDIBILITY_EVENTS:
            continue  # an EQ or filter move cannot change whether a deck is heard
        cuts.add(float(event.at_bar))
        if event.kind == "ramp":
            cuts.add(float(event.at_bar + event.over_bars))
            span = max(1, math.ceil(event.over_bars * SILENCE_SAMPLES_PER_RAMP_BAR))
            cuts.update(event.at_bar + event.over_bars * step / span for step in range(span + 1))
    return cuts


def _silent_bars(
    plan: TransitionPlan,
    start: tuple[float, tuple[ChannelStart, ChannelStart]],
    assigns: tuple[Any, Any],
) -> set[int]:
    """The bars where neither deck is audible at some point.

    Audibility is sampled on a grid of ``SILENCE_SAMPLES_PER_BAR`` per bar, plus every event's own bar, every
    ramp's end, and a ``SILENCE_SAMPLES_PER_RAMP_BAR`` grid across each ramp's span. Wherever two neighboring
    samples disagree, bisection finds the instant it flips (a fader ramp crossing the audible floor between
    samples, for one), so every stretch between the resulting cut
    points is uniformly heard or silent, and its midpoint decides it.
    """
    crossfader, channels = start
    events = _replay_events(plan, channels)

    def audible(at: float) -> bool:
        state = _state_of(plan, events, at, min(int(at), plan.bars - 1), crossfader, channels)
        return any(_heard(state, *assigns))

    points = sorted(cut for cut in _silence_cuts(plan) if 0.0 <= cut <= plan.bars)
    heard = {point: audible(point) for point in points}
    bounds = [points[0]]
    for lo, hi in pairwise(points):
        if heard[lo] != heard[hi]:
            low, high = lo, hi
            for _ in range(BOUNDARY_BISECTIONS):
                middle = (low + high) / 2
                if audible(middle) == heard[lo]:
                    low = middle
                else:
                    high = middle
            bounds.append(high)
        bounds.append(hi)
    silent: set[int] = set()
    for lo, hi in pairwise(bounds):
        # Only float rounding is dropped: a flip on an event's own instant leaves a stretch of that size.
        if hi - lo > SILENCE_EPSILON_BARS and not audible((lo + hi) / 2):
            silent.add(min(int(lo), plan.bars - 1))
    return silent


def _check_end(
    plan: TransitionPlan,
    start: tuple[float, tuple[ChannelStart, ChannelStart]],
    assigns: tuple[Any, Any],
    result: Validation,
) -> None:
    """The bars are sampled at their middles, so the events on the last bar itself
    (the handover's stops) are checked on the state they leave."""
    crossfader, channels = start
    end = state_at(plan, float(plan.bars), bar=plan.bars, crossfader=crossfader, channels=channels)
    _, in_heard = _heard(end, *assigns)
    # The outgoing deck is judged on the page's silence threshold, not the audible floor the bars count with: a fader
    # the page still hears would keep both decks running after a plan that reports done.
    out_gain = end.out_fader * end.out_trim / UNITY_TRIM * _side_gain(assigns[0], end.crossfader)
    if end.out_playing and out_gain >= SILENT_GAIN:
        # A plan that leaves the outgoing deck playing and heard never hands the room over: it would report done
        # while both decks run on, so the handover has to stop it or fade it to silence.
        result.errors.append(
            f"the plan ends at bar {plan.bars} with the outgoing deck {plan.out.deck} still heard: end it with a stop "
            "or a fader to silence, so the room is handed over"
        )
    if not in_heard:
        result.errors.append(
            f"the plan ends at bar {plan.bars} with the incoming deck {plan.in_.deck} unheard: the room "
            "would go silent after the handover"
        )
    # The handover makes the incoming deck master: the plan's last master move, in the order it fires, must be the incoming
    # deck on the last bar. Otherwise the plan reports done with the master on a deck it stopped, and the next proposal
    # would take that deck as its outgoing source. The sort is stable, so equal bars keep their authored order.
    masters = sorted((e for e in plan.events if e.name == "master"), key=lambda e: e.at_bar)
    last = masters[-1] if masters else None
    if last is None or last.deck != plan.in_.deck or last.at_bar < plan.bars:
        result.errors.append(
            f"the plan ends at bar {plan.bars} without making the incoming deck {plan.in_.deck} master: the handover "
            "needs its last master move to be the incoming deck on the last bar"
        )


def _report_clashes(
    out_profile: TrackProfile, in_profile: TrackProfile, counts: tuple[int, int, int], result: Validation
) -> None:
    vocal_overlap, bass_overlap, silent_bars = counts
    vocals_known = out_profile.bar_vocals is not None and in_profile.bar_vocals is not None
    bass_known = out_profile.bar_bass is not None and in_profile.bar_bass is not None
    result.metrics["vocal_overlap_bars"] = vocal_overlap if vocals_known else None
    result.metrics["bass_overlap_bars"] = bass_overlap if bass_known else None
    result.metrics["silent_bars"] = silent_bars
    if vocals_known and vocal_overlap:
        result.warnings.append(f"two vocals overlap for {vocal_overlap} bars")
    if not vocals_known:
        result.warnings.append("vocals are not analyzed on one side: vocal clashes were not checked")
    if in_profile.bar_bass is None:
        result.warnings.append(
            "the incoming track has no bar_bass series; the bass hit was not placed from the music"
        )
    if bass_known and bass_overlap > 1:
        result.warnings.append(f"two basslines play together for {bass_overlap} bars")
    if silent_bars:
        result.errors.append(f"{silent_bars} bars of the plan have neither deck audible")


def live_mapping(value: Any, where: str = "live mirror value") -> Mapping[str, Any]:
    """AGENT-35: an object level of the live mirror (mixer, channels, a channel, decks, a deck, a position) that is
    PRESENT. Anything that is not an object, explicit null included, is a corrupt mirror and raises."""
    if not isinstance(value, Mapping):
        raise ValueError(f"{where} is {'null' if value is None else type(value).__name__}, not an object")  # noqa: TRY004 - one ValueError for every corrupt mirror value
    return value


def live_child(parent: Mapping[str, Any], key: str, where: str = "live mirror value") -> Mapping[str, Any]:
    """AGENT-35: the ONE reader for a child object level of the live mirror. A MISSING key reads as an empty object, so
    its values read as absent; a present key goes through :func:`live_mapping`, so an explicit null raises like any
    other malformed value (Sol P1 #7595: null is malformed, not missing)."""
    if key not in parent:
        return {}
    return live_mapping(parent[key], where)


def _loop_engaged(state: Mapping[str, Any]) -> bool:
    """Whether the HTTP UI mirror reports an engaged loop (``loop.engaged`` or a legacy top-level flag)."""
    if state.get("loop_engaged") is True:
        return True
    loop = state.get("loop")
    return isinstance(loop, Mapping) and loop.get("engaged") is True


def _validate_live(
    plan: TransitionPlan, profiles: tuple[TrackProfile, TrackProfile], live: Mapping[str, Any], result: Validation
) -> None:
    """Checks against the AGENT-02 mirror: who holds what, where the out deck is."""
    out_profile, in_profile = profiles
    decks = live_child(live, "decks", "live mirror decks")
    out_state = live_child(decks, str(plan.out.deck), "live mirror deck")
    in_state = live_child(decks, str(plan.in_.deck), "live mirror deck")
    if not isinstance(out_state, Mapping) or out_state.get("stable_id") != plan.out.track:
        result.errors.append(f"deck {plan.out.deck} does not hold the outgoing track {plan.out.track}")
        return
    if out_state.get("playing") is not True:
        result.errors.append(
            f"deck {plan.out.deck} is not playing: a plan continues a playing set; start the "
            "outgoing deck first with the command tool"
        )
    if isinstance(in_state, Mapping) and in_state.get("playing") is True:
        result.errors.append(f"deck {plan.in_.deck} is already playing; the incoming deck must be cued, not live")
    if isinstance(in_state, Mapping) and in_state.get("quantized_launch_armed") is not None:
        # Schedule and the page's runner refuse an armed deck as busy, so validation must refuse it too.
        result.errors.append(f"deck {plan.in_.deck} is armed to launch; pick an idle deck")
    if _loop_engaged(out_state):
        # An engaged loop wraps the playhead, so the outgoing window is never reached and the plan would wait forever.
        result.errors.append(f"deck {plan.out.deck} has an engaged loop; release it so the plan can reach its window")
    if isinstance(in_state, Mapping) and _loop_engaged(in_state):
        # The seek lands inside the loop; quantized launch keeps it, so the deck repeats loop bars instead of the plan window.
        result.errors.append(f"deck {plan.in_.deck} has an engaged loop; release it so the plan can play its window")
    _check_out_position(plan, out_profile, out_state, result)
    _check_crossfader(plan, live_child(live, "mixer"), result)
    _check_pitch_reach(plan, in_profile, (out_state, in_state), result)
    _check_authored_tempo(plan, plan.in_.deck, in_state, result)
    _check_authored_tempo(plan, plan.out.deck, out_state, result)
    for event in plan.events:
        if event.name in PAGE_PENDING_FX:
            result.errors.append(
                f"{event.name} at bar {event.at_bar:g}: the page executor has no FX plan controls yet (#9848), "
                "so this plan cannot run live"
            )


def _beatmatched(plan: TransitionPlan) -> bool:
    style_doc = STYLES.get(plan.style)
    return (style_doc.needs_beatmatch if style_doc is not None else True) and plan.bars > 1


def sync_tempo_fold(out_state: Mapping[str, Any], in_profile: TrackProfile, range_pct: float) -> float | None:
    """The fold Beat Sync would pick to match the incoming deck to the outgoing one (1, 0.5 or 2).

    Beat Sync tries the raw ratio, then half and double time, and takes the first inside
    the deck's +-``range_pct`` pitch range; ``None`` when none fits or a tempo is unknown.
    """
    out_bpm = out_state.get("effective_bpm")
    if not isinstance(out_bpm, (int, float)) or out_bpm <= 0 or not in_profile.bpm or in_profile.bpm <= 0:
        return None
    raw = float(out_bpm) / in_profile.bpm
    for fold in TEMPO_FOLDS:
        if abs(raw * fold - 1.0) * 100.0 <= range_pct + 1e-9:
            return fold
    return None


def sync_tempo_ratio(out_state: Mapping[str, Any], in_profile: TrackProfile, range_pct: float) -> float | None:
    """The incoming deck's tempo ratio that Beat Sync would pick to match the outgoing deck (see :func:`sync_tempo_fold`)."""
    fold = sync_tempo_fold(out_state, in_profile, range_pct)
    if fold is None:
        return None
    return float(out_state["effective_bpm"]) / in_profile.bpm * fold


def _check_authored_tempo(
    plan: TransitionPlan, deck: int | None, deck_state: Mapping[str, Any], result: Validation
) -> None:
    """An authored tempo is an absolute ratio the addressed deck's pitch range must reach, or setTempoRatio refuses it.

    Checked for both decks: the clock deck's tempo moves reach the page's ``setTempoRatio`` just the same.
    """
    targets = [
        e for e in plan.events if e.kind != "do" and e.deck == deck and e.name == "tempo" and e.value is not None
    ]
    if not targets:
        return
    range_pct = deck_state.get("pitch_range")
    if not isinstance(range_pct, (int, float)):
        result.warnings.append(
            f"deck {deck}'s pitch range is unknown: authored tempo targets were not checked against it"
        )
        return
    for event in targets:
        if abs(float(event.value) - 1.0) * 100.0 > float(range_pct) + 1e-9:
            result.errors.append(
                f"tempo {float(event.value):g} at bar {event.at_bar:g} is outside deck {deck}'s "
                f"+-{float(range_pct):g}% pitch range"
            )


def _check_pitch_reach(
    plan: TransitionPlan,
    in_profile: TrackProfile,
    states: tuple[Mapping[str, Any], Mapping[str, Any]],
    result: Validation,
) -> None:
    """A beatmatched plan needs a tempo the incoming deck's pitch range can reach."""
    if plan.tempo_mode != "sync_to_out" or not _beatmatched(plan):
        return
    out_state, in_state = states
    range_pct = in_state.get("pitch_range")
    out_bpm = out_state.get("effective_bpm")
    if not isinstance(range_pct, (int, float)) or not isinstance(out_bpm, (int, float)) or not in_profile.bpm:
        result.warnings.append(
            f"deck {plan.in_.deck}'s pitch range or a tempo is unknown: whether Beat Sync can reach "
            "the outgoing tempo was not checked"
        )
        return
    if sync_tempo_ratio(out_state, in_profile, float(range_pct)) is None:
        result.errors.append(
            f"deck {plan.in_.deck} cannot match {float(out_bpm):.2f} BPM from {in_profile.bpm:.2f} within its "
            f"+-{float(range_pct):g}% pitch range, even at half or double time: widen the range or pick "
            "a style that does not beatmatch (cut, drop_swap)"
        )


def _check_out_position(
    plan: TransitionPlan, out_profile: TrackProfile, out_state: Mapping[str, Any], result: Validation
) -> None:
    position_ms = live_child(out_state, "position").get("ms")
    if not isinstance(position_ms, (int, float)):
        return
    bar_now = out_profile.bar_at(float(position_ms) / 1000.0)
    result.metrics["out_bar_now"] = round(bar_now, 2)
    if plan.out.start_bar + first_dispatch_bar(plan) <= bar_now + OUT_POSITION_MARGIN_BARS:
        result.errors.append(
            f"the out window starts at bar {plan.out.start_bar} but deck {plan.out.deck} is "
            f"already at bar {bar_now:.1f}; pick a later window or anchor next_phrase"
        )


def _check_crossfader(plan: TransitionPlan, mixer: Mapping[str, Any], result: Validation) -> None:
    channels = mixer.get("channels")
    crossfader = mixer.get("crossfader")
    if not isinstance(channels, Mapping) or not isinstance(crossfader, (int, float)):
        return
    assign = _assign_of(mixer, plan.in_.deck)
    # Where the plan leaves it: a plan may open the incoming side itself (a crossfader mix).
    end = state_at(plan, float(plan.bars), bar=plan.bars, crossfader=float(crossfader)).crossfader
    if not _side_open(assign, end):
        moved = "" if end == crossfader else f" (the plan moves it from {crossfader:.2f})"
        result.errors.append(
            f"deck {plan.in_.deck} is assigned to side {assign} and the crossfader ends at "
            f"{end:.2f}{moved}: the incoming deck would not be heard and the room would go "
            "silent when the outgoing deck stops; move the crossfader or reassign the channel first"
        )
