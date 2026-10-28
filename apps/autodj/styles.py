"""The transition style catalog (``list_transition_styles``) and its builders.

A style turns (overlap length, which deck is which) into plan events on the
plan's own bar clock: bar 0 is the outgoing window's first bar, and the plan
ends at ``bars``. Builders only emit controls the page executor can move
(:data:`apps.autodj.plan.CONTROLS`), so a style that needs something the app
does not have yet (FX DSP, stems in plan execution) is listed with
``available: false`` and the reason, never faked.

Values are bus control units: 0..1 with 0.5 flat for EQ, TRIM and FILTER;
FILTER below 0.5 sweeps a lowpass closed, above 0.5 a highpass
(``apps/webui/frontend/src/lib/rb/mixer-types.ts``).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

Event = dict[str, Any]
Builder = Callable[[int, int, int], list[Event]]

FLAT = 0.5
KILL = 0.0
HIGHPASS_SWEEP = 0.85
#: The lowpass end of a sweep, mirrored about FLAT. Hygiene, mimicked: the FILTER knob's taper is the in-repo law
#: that tracks Mixxx's ((private path)), so 0.15 and 0.85 are equally deep cuts each way.
LOWPASS_SWEEP = 0.15
#: Linear ramp segments one shaped fade compiles to (no schema change: a curve is a chain of ``ramp`` events).
#: Eight puts a breakpoint on the midpoint, so an equal-power fade reads exactly cos(pi/4) = 0.707 there.
CURVE_SEGMENTS = 8

# ----- style parameters (AGENT-34) -------------------------------------------
#: Fade laws. Equal power is the default, mimicked from Spotify's Smooth crossfade and Mixxx's constant-power
#: crossfader curve (each deck at -3 dB, 0.707, mid-fade); linear (-6 dB, 0.5, mid-fade) stays available.
FADE_SHAPES = ("equal_power", "linear")
#: The fade every built-in blend uses (AGENT-34 round 4): the old hand-built blends too, not only parameter points.
DEFAULT_FADE_SHAPE = "equal_power"
#: Overlap compensation, hygiene mimicked from transitions spec 01 (01-volume-lane-transitions.md, parameters table
#: and trap 4, research SHA 3336884c1e): -3 dB on BOTH decks while two full-band decks overlap with no low-EQ kill
#: active, because the page has no limiter (only the #1475 static ceiling) and two summed masters run over 0 dBTP.
DEFAULT_OVERLAP_COMP_DB = -3.0
#: Round 5 of the try-er experiment log ((private path)): compensate bass-swap overlaps too. This DEPARTS
#: from spec 01, which compensates only while no low-EQ kill is active, because the round 4 bench on the real smoke pair
#: (n = 24) still measured +1.08 to +1.99 dBTP overs on every bass-swap style: with the lows of one deck killed, the
#: two decks' mids and highs still sum over the ceiling. A parameter, so the spec's rule is one flag away.
COMP_WITH_LOW_KILL = True
#: How far before the window the outgoing deck eases down to the compensated level: one bar, the plan's earliest bar
#: (``plan.EARLIEST_BAR``), so it is there before the incoming deck is heard.
PRE_COMP_LEAD_BARS = 1.0
#: Volume lanes where both decks sit near full together, so the sum needs compensation (a crossfade is already
#: constant power by its law).
COMPENSATED_LANES = ("overlap", "fade_in_fade_out")
#: Volume lanes, named as docs/glossary.md "Transitions and mixing" (Spotify's Volume options).
VOLUME_LANES = ("crossfade", "overlap", "fade_in_fade_out")
#: Where the bass swap lands: the region's center (mix point) or its end (Spotify "Centre"/"End bass swap").
SWAP_POSITIONS = ("center", "end")
#: Filter primitives: which deck is swept and which way (glossary: low-pass in, high-pass out, ...).
FILTER_MOVES = ("lp_in_hp_out", "lp_both", "hp_both")
#: Stem moves: a parameter, but no style with one is available until stems are part of plan execution.
STEM_MOVES = ("vocals_swap",)


@dataclass(frozen=True)
class Style:
    """One named transition and the knobs it moves."""

    name: str
    summary: str
    min_bars: int
    max_bars: int
    default_bars: int
    needs_beatmatch: bool
    controls: tuple[str, ...]
    when: str
    available: bool = True
    unavailable_reason: str | None = None
    builder: Builder | None = field(default=None, repr=False, compare=False)
    #: AGENT-34: the parameter point a parametric style is built from; None for a hand-built style.
    params: StyleParams | None = None
    #: The Spotify preset this style reproduces (docs/glossary.md), or None.
    spotify: str | None = None

    def clamp_bars(self, bars: int) -> int:
        return max(self.min_bars, min(self.max_bars, bars))

    def build(self, bars: int, out_deck: int, in_deck: int) -> list[Event]:
        if not self.available or self.builder is None:
            raise ValueError(f"style {self.name!r} is not available: {self.unavailable_reason}")
        return self.builder(self.clamp_bars(bars), out_deck, in_deck)

    def summary_doc(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "summary": self.summary,
            "bars": {"min": self.min_bars, "max": self.max_bars, "default": self.default_bars},
            "needs_beatmatch": self.needs_beatmatch,
            "controls": list(self.controls),
            "when": self.when,
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
            "params": None if self.params is None else self.params.doc(),
            "spotify": self.spotify,
        }


def _set(at_bar: float, deck: int | None, control: str, value: float) -> Event:
    return {"at_bar": at_bar, "deck": deck, "set": {control: value}}


def _ramp(at_bar: float, deck: int | None, control: str, value: float, over_bars: float) -> Event:
    return {"at_bar": at_bar, "deck": deck, "ramp": {control: value}, "over_bars": over_bars}


def _do(at_bar: float, deck: int, action: str) -> Event:
    return {"at_bar": at_bar, "deck": deck, "do": action}


def _handover(bars: int, out_deck: int, in_deck: int) -> list[Event]:
    """Every style ends the same way: the incoming deck is master, the outgoing
    deck stops, and its strip is put back to neutral so the next track loaded
    there (by AutoPlay or anyone) is not silently EQ-killed or faded out."""
    return [
        _do(bars, in_deck, "master"),
        _do(bars, out_deck, "stop"),
        _set(bars, out_deck, "eq_low", FLAT),
        _set(bars, out_deck, "eq_mid", FLAT),
        _set(bars, out_deck, "eq_high", FLAT),
        _set(bars, out_deck, "filter", FLAT),
        _set(bars, out_deck, "fader", 1.0),
        # Both decks' filters: a style that sweeps the incoming deck must not leave the new master filtered.
        _set(bars, in_deck, "filter", FLAT),
    ]


def _cut(bars: int, out_deck: int, in_deck: int) -> list[Event]:
    return [
        _set(0, in_deck, "fader", 1.0),
        _set(0, in_deck, "eq_low", FLAT),
        _do(0, in_deck, "play"),
        _set(0, out_deck, "fader", KILL),
        *_handover(bars, out_deck, in_deck),
    ]


def _filter_sweep(bars: int, out_deck: int, in_deck: int) -> list[Event]:
    half = max(1, bars // 2)
    return [
        _set(0, in_deck, "fader", KILL),
        _set(0, in_deck, "eq_low", KILL),
        _do(0, in_deck, "play"),
        *_fader_moves((in_deck, out_deck), ((0, half), (half, bars - half)), DEFAULT_FADE_SHAPE, _hand_built_comp()),
        _ramp(0, out_deck, "filter", HIGHPASS_SWEEP, bars),
        _set(half, in_deck, "eq_low", FLAT),
        _set(half, out_deck, "eq_low", KILL),
        *_handover(bars, out_deck, in_deck),
    ]


def _hand_built_comp() -> float:
    """The overlap compensation the hand-built bass-swap blends use: round 5 turns it on for them (COMP_WITH_LOW_KILL)."""
    return DEFAULT_OVERLAP_COMP_DB if COMP_WITH_LOW_KILL else 0.0


def _bass_swap_blend(bars: int, out_deck: int, in_deck: int) -> list[Event]:
    half = max(1, bars // 2)
    fade_in = max(1, bars // 4)
    fade_out_from = bars - max(1, bars // 4)
    return [
        _set(0, in_deck, "fader", KILL),
        _set(0, in_deck, "eq_low", KILL),
        _do(0, in_deck, "play"),
        *_fader_moves(
            (in_deck, out_deck),
            ((0, fade_in), (fade_out_from, bars - fade_out_from)),
            DEFAULT_FADE_SHAPE,
            _hand_built_comp(),
        ),
        # One bassline at a time: swap the lows on the phrase midpoint.
        _set(half, out_deck, "eq_low", KILL),
        _set(half, in_deck, "eq_low", FLAT),
        *_handover(bars, out_deck, in_deck),
    ]


def _long_blend(bars: int, out_deck: int, in_deck: int) -> list[Event]:
    half = max(1, bars // 2)
    quarter = max(1, bars // 4)
    return [
        _set(0, in_deck, "fader", KILL),
        _set(0, in_deck, "eq_low", KILL),
        _set(0, in_deck, "eq_high", 0.35),
        _do(0, in_deck, "play"),
        *_fader_moves((in_deck, out_deck), ((0, half), (half, bars - half)), DEFAULT_FADE_SHAPE, _hand_built_comp()),
        _ramp(quarter, in_deck, "eq_high", FLAT, quarter),
        _set(half, out_deck, "eq_low", KILL),
        _set(half, in_deck, "eq_low", FLAT),
        _ramp(half, out_deck, "eq_high", 0.35, quarter),
        *_handover(bars, out_deck, in_deck),
    ]


def _drop_swap(bars: int, out_deck: int, in_deck: int) -> list[Event]:
    # The incoming window starts ON its drop; the outgoing deck is cut on the
    # same downbeat. Same mechanics as a cut, different window choice.
    return _cut(bars, out_deck, in_deck)


@dataclass(frozen=True)
class StyleParams:
    """One point in the style space: the parameters a parametric style is built from."""

    volume: str
    fade_shape: str = "equal_power"
    swap_position: str | None = "center"
    filter: str | None = None
    stems: str | None = None
    overlap_comp_db: float = DEFAULT_OVERLAP_COMP_DB
    comp_with_low_kill: bool = COMP_WITH_LOW_KILL

    def __post_init__(self) -> None:
        if not -6.0 <= self.overlap_comp_db <= 0.0:
            raise ValueError(f"overlap_comp_db {self.overlap_comp_db} is outside spec 01's -6 to 0 dB")
        for name, value, allowed in (
            ("volume", self.volume, VOLUME_LANES),
            ("fade_shape", self.fade_shape, FADE_SHAPES),
            ("swap_position", self.swap_position, (*SWAP_POSITIONS, None)),
            ("filter", self.filter, (*FILTER_MOVES, None)),
            ("stems", self.stems, (*STEM_MOVES, None)),
        ):
            if value not in allowed:
                raise ValueError(f"{name} {value!r} is not one of {allowed}")

    def doc(self) -> dict[str, Any]:
        return {
            "volume": self.volume,
            "fade_shape": self.fade_shape,
            "swap_position": self.swap_position,
            "filter": self.filter,
            "stems": self.stems,
            "overlap_comp_db": self.overlap_comp_db,
            "comp_with_low_kill": self.comp_with_low_kill,
        }

    def compensates(self) -> bool:
        """Spec 01 compensates an overlap with no low-EQ kill; round 5 extends it to bass swaps (``comp_with_low_kill``)."""
        no_kill = self.swap_position is None or self.comp_with_low_kill
        return self.volume in COMPENSATED_LANES and no_kill and self.overlap_comp_db < 0.0


def _gain(shape: str, t: float, rising: bool) -> float:
    """Fader gain at fade progress ``t`` (0..1): equal power is sin/cos of a quarter turn, linear is ``t``."""
    if shape == "equal_power":
        return math.sin(t * math.pi / 2) if rising else math.cos(t * math.pi / 2)
    if shape == "linear":
        return t if rising else 1.0 - t
    raise ValueError(f"unknown fade shape {shape!r}")


def _shaped_ramp(deck: int, control: str, start: float, span: float, shape: str, rising: bool) -> list[Event]:
    """A shaped fade as a chain of linear ramps, one per CURVE_SEGMENTS breakpoint."""
    step = span / CURVE_SEGMENTS
    return [
        _ramp(start + k * step, deck, control, round(_gain(shape, (k + 1) / CURVE_SEGMENTS, rising), 6), step)
        for k in range(CURVE_SEGMENTS)
    ]


def _fade_spans(volume: str, bars: int) -> tuple[tuple[float, float], tuple[float, float]]:
    """(start, span) of the incoming fade-in and the outgoing fade-out for a volume lane (glossary shapes:
    crossfade both across the region; overlap B up in the first quarter, A out in the last quarter, inferred from
    the name; fade in fade out B up over the first half, A out over the second, traced from Spotify's editor)."""
    if volume == "crossfade":
        return (0.0, bars), (0.0, bars)
    if volume == "overlap":
        quarter = bars / 4
        return (0.0, quarter), (bars - quarter, quarter)
    if volume == "fade_in_fade_out":
        half = bars / 2
        return (0.0, half), (half, half)
    raise ValueError(f"unknown volume lane {volume!r}")


def _filter_events(
    move: str | None, out_deck: int, in_deck: int, spans: tuple[tuple[float, float], tuple[float, float]]
) -> list[Event]:
    """Sweep the incoming deck open across its fade-in and the outgoing deck closed across its fade-out."""
    if move is None:
        return []
    (in_start, in_span), (out_start, out_span) = spans
    in_from, out_to = {
        "lp_in_hp_out": (LOWPASS_SWEEP, HIGHPASS_SWEEP),
        "lp_both": (LOWPASS_SWEEP, LOWPASS_SWEEP),
        "hp_both": (HIGHPASS_SWEEP, HIGHPASS_SWEEP),
    }[move]
    return [
        _set(0, in_deck, "filter", in_from),
        _ramp(in_start, in_deck, "filter", FLAT, in_span),
        _ramp(out_start, out_deck, "filter", out_to, out_span),
    ]


def _fader_moves(
    decks: tuple[int, int], spans: tuple[tuple[float, float], tuple[float, float]], shape: str, comp_db: float
) -> list[Event]:
    """The two decks' fader fades: plain shaped ramps, or held ``comp_db`` down through the overlap when it is < 0."""
    if comp_db < 0.0:
        return _compensated_overlap(
            StyleParams(volume="overlap", fade_shape=shape, overlap_comp_db=comp_db), decks, spans
        )
    (in_start, in_span), (out_start, out_span) = spans
    in_deck, out_deck = decks
    return [
        *_shaped_ramp(in_deck, "fader", in_start, in_span, shape, rising=True),
        *_shaped_ramp(out_deck, "fader", out_start, out_span, shape, rising=False),
    ]


def _compensated_overlap(
    params: StyleParams, decks: tuple[int, int], spans: tuple[tuple[float, float], tuple[float, float]]
) -> list[Event]:
    """Both decks held ``overlap_comp_db`` down while they overlap at full band, as linear ramp chains (no schema
    change): the incoming deck fades in to the compensated level, the outgoing deck eases down to it over the same span,
    and once the outgoing deck has faded to silence the incoming one comes back up to full."""
    in_deck, out_deck = decks
    (in_start, in_span), (out_start, out_span) = spans
    level = round(10.0 ** (params.overlap_comp_db / 20.0), 6)
    step = in_span / CURVE_SEGMENTS
    rising = [
        _ramp(
            in_start + k * step,
            in_deck,
            "fader",
            round(level * _gain(params.fade_shape, (k + 1) / CURVE_SEGMENTS, True), 6),
            step,
        )
        for k in range(CURVE_SEGMENTS)
    ]
    # The outgoing fade takes the first half of its span and the incoming deck's return to full the second half, so the
    # two never overlap: while both decks are heard, each is at or below the level.
    restore = out_span / 2
    out_step = (out_span - restore) / CURVE_SEGMENTS
    falling = [
        _ramp(
            out_start + k * out_step,
            out_deck,
            "fader",
            round(level * _gain(params.fade_shape, (k + 1) / CURVE_SEGMENTS, False), 6),
            out_step,
        )
        for k in range(CURVE_SEGMENTS)
    ]
    # The outgoing deck is already at the compensated level when the incoming one starts (it eases down in the one
    # bar before the window, the plan's earliest bar), and the incoming deck comes back to full only after the
    # outgoing deck has reached silence: so while both are heard, neither deck is above the level and their sum never
    # exceeds the overlap core's 2 x level.
    return [
        _ramp(in_start - PRE_COMP_LEAD_BARS, out_deck, "fader", level, PRE_COMP_LEAD_BARS),
        *rising,
        *falling,
        _ramp(out_start + out_span - restore, in_deck, "fader", 1.0, restore),
    ]


def parametric_builder(params: StyleParams) -> Builder:
    """The builder for one point in the style space (AGENT-34). Bars are the plan's own clock (bar 0 = out window)."""

    def build(bars: int, out_deck: int, in_deck: int) -> list[Event]:
        if params.stems is not None:
            raise ValueError("stem moves are not part of plan execution yet")
        spans = _fade_spans(params.volume, bars)
        events = [_set(0, in_deck, "fader", KILL), _do(0, in_deck, "play")]
        comp_db = params.overlap_comp_db if params.compensates() else 0.0
        events += _fader_moves((in_deck, out_deck), spans, params.fade_shape, comp_db)
        if params.swap_position is not None:
            # One bassline at a time: the incoming lows wait killed until the swap bar.
            # Center swaps on the mix point; end swaps on the transition's last downbeat (Spotify "End bass swap"),
            # where the outgoing deck stops and the handover resets its lows, so only the incoming lows open there.
            events.append(_set(0, in_deck, "eq_low", KILL))
            if params.swap_position == "center":
                events += [_set(bars / 2, out_deck, "eq_low", KILL), _set(bars / 2, in_deck, "eq_low", FLAT)]
            elif params.swap_position == "end":
                pass  # opened after the handover's outgoing stop, below (Sol P1 #7595: one bassline, even within a bar)
            else:
                raise ValueError(f"unknown swap position {params.swap_position!r}")
        events += _filter_events(params.filter, out_deck, in_deck, spans)
        # Same-bar events run in plan order, so the end swap's incoming lows open only after the outgoing deck stops.
        opens_after = [_set(bars, in_deck, "eq_low", FLAT)] if params.swap_position == "end" else []
        return [*events, *_handover(bars, out_deck, in_deck), *opens_after]

    return build


def _parametric(
    name: str, summary: str, when: str, params: StyleParams, spotify: str | None, bars: tuple[int, int, int]
) -> Style:
    controls = ["fader", "play", "master"]
    if params.swap_position is not None:
        controls.append("eq_low")
    if params.filter is not None:
        controls.append("filter")
    unavailable = "stems are not part of plan execution yet (canon order of work, step 4)" if params.stems else None
    return Style(
        name=name,
        summary=summary,
        min_bars=bars[0],
        max_bars=bars[1],
        default_bars=bars[2],
        needs_beatmatch=True,
        controls=tuple(controls) + (("stem_gain",) if params.stems else ()),
        when=when,
        available=unavailable is None,
        unavailable_reason=unavailable,
        builder=parametric_builder(params),
        params=params,
        spotify=spotify,
    )


_STYLES: tuple[Style, ...] = (
    Style(
        name="cut",
        summary="Hard cut on a downbeat: the incoming deck starts as the outgoing one is muted (Spotify Slam).",
        min_bars=1,
        max_bars=1,
        default_bars=1,
        needs_beatmatch=False,
        controls=("fader", "play", "master"),
        when="Big key or tempo gaps, showy moments, or when the outgoing track ends abruptly.",
        builder=_cut,
        spotify="Slam",
    ),
    Style(
        name="drop_swap",
        summary="The incoming track enters ON its drop at the outgoing phrase end; the outgoing deck cuts.",
        min_bars=1,
        max_bars=1,
        default_bars=1,
        needs_beatmatch=True,
        controls=("fader", "play", "master"),
        when="Keep the energy up: land the next drop where the current phrase ends.",
        builder=_drop_swap,
    ),
    Style(
        name="filter_sweep",
        summary="Highpass the outgoing track while the incoming one fades in; bass swaps at the midpoint.",
        min_bars=4,
        max_bars=16,
        default_bars=8,
        needs_beatmatch=True,
        controls=("filter", "fader", "eq_low", "play", "master"),
        when="Short, energetic transitions; hides a small key clash.",
        builder=_filter_sweep,
    ),
    Style(
        name="bass_swap_blend",
        summary="Beatmatched blend with the incoming lows killed, swapped on the phrase midpoint.",
        min_bars=8,
        max_bars=32,
        default_bars=16,
        needs_beatmatch=True,
        controls=("fader", "eq_low", "play", "master"),
        when="The default for compatible tracks: one bassline at a time, 16 to 32 bars.",
        builder=_bass_swap_blend,
    ),
    Style(
        name="long_blend",
        summary="A long blend: highs eased in, lows swapped at the midpoint, the outgoing track fades out.",
        min_bars=32,
        max_bars=64,
        default_bars=32,
        needs_beatmatch=True,
        controls=("fader", "eq_low", "eq_high", "play", "master"),
        when="Smooth or winding-down sets with compatible keys and close tempos.",
        builder=_long_blend,
    ),
    _parametric(
        "crossfade",
        "Equal-power crossfade across the region with a center bass swap (Spotify Fade).",
        "Compatible tracks when you want the smoothest possible handover.",
        StyleParams(volume="crossfade"),
        "Fade",
        (8, 32, 16),
    ),
    _parametric(
        "rise",
        "Overlap with the incoming deck low-passed in, the outgoing high-passed out, bass swapped at the end (Spotify Rise).",
        "Building energy into the next track.",
        StyleParams(volume="overlap", swap_position="end", filter="lp_in_hp_out"),
        "Rise",
        (8, 32, 16),
    ),
    _parametric(
        "wave",
        "Overlap with both decks low-passed (in opening, out closing) and a center bass swap (Spotify Wave).",
        "A dark, rolling handover between deep or melodic tracks.",
        StyleParams(volume="overlap", filter="lp_both"),
        "Wave",
        (8, 32, 16),
    ),
    _parametric(
        "melt",
        "Fade in then fade out, both decks high-passed, center bass swap (Spotify Melt).",
        "Airy handovers; thins both tracks so their lows never meet.",
        StyleParams(volume="fade_in_fade_out", filter="hp_both"),
        "Melt",
        (8, 32, 16),
    ),
    Style(
        name="echo_out",
        summary="Echo the outgoing track out over 1 to 4 bars.",
        min_bars=1,
        max_bars=4,
        default_bars=2,
        needs_beatmatch=False,
        controls=("fx_echo",),
        when="The universal escape for bad pairs.",
        available=False,
        unavailable_reason="no FX DSP yet (pad-mode FX are built:false; (private path))",
    ),
    _parametric(
        "stem_swap",
        "Swap vocals between decks with stems over a crossfade.",
        "Acapella over the next beat; avoiding a vocal clash.",
        StyleParams(volume="crossfade", stems="vocals_swap"),
        None,
        (8, 32, 16),
    ),
    Style(
        name="loop_extend",
        summary="Loop the outgoing phrase to buy bars before the blend.",
        min_bars=8,
        max_bars=32,
        default_bars=16,
        needs_beatmatch=True,
        controls=("beat_loop",),
        when="The outgoing outro is too short for the blend you want.",
        available=False,
        unavailable_reason="amend_plan hold is not built yet; use extend before the plan starts",
    ),
)

STYLES: dict[str, Style] = {style.name: style for style in _STYLES}


def list_styles() -> list[dict[str, Any]]:
    return [style.summary_doc() for style in _STYLES]


def style(name: str) -> Style:
    try:
        return STYLES[name]
    except KeyError:
        raise ValueError(f"unknown style {name!r}; one of {', '.join(STYLES)}") from None
