"""The transition style catalog (``list_transition_styles``) and its builders.

A style turns (overlap length, which deck is which) into plan events on the
plan's own bar clock: bar 0 is the outgoing window's first bar, and the plan
ends at ``bars``. Builders only emit controls the page executor can move
(:data:`apps.autodj.plan.CONTROLS`), so a style that needs something the app
does not have yet (FX DSP, stems in plan execution) is listed with
``available: false`` and the reason, never faked.

Every style gives way around the incoming track's bass hit (D11, D12 in
(private path)): nothing on the outgoing deck (a low kill, a body
fade, a filter, a cut) happens before the incoming bass has landed; the
outgoing body then fades over at least ``min_body_fade_bars`` with an
equal-power law, so the two decks never sum above one full deck.

Values are bus control units: 0..1 with 0.5 flat for EQ, TRIM and FILTER;
FILTER below 0.5 sweeps a lowpass closed, above 0.5 a highpass
(``apps/webui/frontend/src/lib/rb/mixer-types.ts``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Protocol

Event = dict[str, Any]


class Builder(Protocol):
    """A style's event builder; ``hit_bar`` is the plan bar the incoming bass lands on (D11), None for the style's own."""

    def __call__(self, bars: int, out_deck: int, in_deck: int, hit_bar: float | None = None, /) -> list[Event]: ...


FLAT = 0.5
KILL = 0.0
HIGHPASS_SWEEP = 0.85
#: The lowpass end of a sweep, mirrored about FLAT. Hygiene, mimicked: the FILTER knob's taper is the in-repo law
#: that tracks Mixxx's ((private path)), so 0.15 and 0.85 are equally deep cuts each way.
LOWPASS_SWEEP = 0.15
#: The highs a long blend eases: the incoming deck's in from here by the hit, the outgoing deck's out to here.
EASED_HIGHS = 0.35
#: Linear ramp segments one shaped fade compiles to (no schema change: a curve is a chain of ``ramp`` events).
#: Eight puts a breakpoint on the midpoint, so an equal-power fade reads exactly cos(pi/4) = 0.707 there.
CURVE_SEGMENTS = 8

# ----- style parameters (AGENT-34) -------------------------------------------
#: Fade laws. Equal power is the default, mimicked from Mixxx's constant-power crossfader curve (each deck at -3 dB,
#: 0.707, mid-fade); ``smooth`` is equal power on cubic ease ``s = 3t^2 - 2t^3`` (Spotify Smooth); linear (-6 dB, 0.5,
#: mid-fade) stays available; ``dipped`` is Melt / spec 01 s1 -- deliberate ~-3 dB power at overlap center (x=0.5).
FADE_SHAPES = ("equal_power", "linear", "smooth", "dipped")
#: The fade every built-in blend uses (AGENT-34 round 4): the old hand-built blends too, not only parameter points.
DEFAULT_FADE_SHAPE = "equal_power"
#: Volume lanes, named as docs/glossary.md "Transitions and mixing" (Spotify's Volume options). Under D11 a lane only
#: says where the bass hit lands when the incoming bass is already there (``_LANE_HIT``); every lane fades the
#: outgoing body from the hit to the handover (D12).
VOLUME_LANES = ("crossfade", "overlap", "fade_in_fade_out")
#: Where the incoming bass lands: "center" gates it inside the blend (D11), "end" lands it ON the handover with the
#: outgoing deck held in its lull until then (lull_then_drop, D13).
SWAP_POSITIONS = ("center", "end", "phrase")
#: Filter primitives: which deck is swept and which way (glossary: low-pass in, high-pass out, ...). ``hp_out``
#: sweeps only the outgoing deck (filter_sweep); ``lp_out`` closes only the outgoing low-pass, incoming left open.
FILTER_MOVES = ("lp_in_hp_out", "lp_both", "hp_both", "hp_out", "lp_out")
#: Stem moves: a parameter, but no style with one is available until stems are part of plan execution.
STEM_MOVES = ("vocals_swap",)

# ----- the bass-hit gate (D11) and the body fade law (D12) --------------------------------------------------------
#: D11: the incoming fader before its bass hit, lows killed: a little of the incoming, never a dip of the outgoing.
#: The one taste knob of the law: it trades headroom before the hit (two masters summed) against the low end on the
#: hit (the incoming lows land at this level and rise). Starts at -6 dB, each deck's level at a linear crossfader's
#: midpoint; calibrated against the maintainer's verdicts in the try-er rounds.
DEFAULT_PRE_HIT_DB = -6.0
#: D13: lull_then_drop brings in only a little of the incoming while the outgoing deck idles in its lull.
LULL_PRE_HIT_DB = -9.0
#: D12 (the maintainer on p09 and p11, Thu 29 Oct 2026: the outgoing core crossfaded "over a small time"): the outgoing body
#: (fader, filters, highs) fades over at least this many bars, from the hit to the handover.
DEFAULT_MIN_BODY_FADE_BARS = 8
#: D14, the maintainer's low-EQ rule, verbatim (Thu 29 Oct 2026): "my normal rule is to reduce incoming LO eq by 1/3 of a dial
#: turn (1/6 of a full turn, 1/3 of a half turn) and bring that up to 1 by the time outgoing is down to 2/3 (where
#: incomign just was). always bass-swappingwill get old fast." On our knob (0.5 flat, 0 = -26 dB, linear in dB) a third
#: of the half turn below flat is 1/3, about -8.7 dB: the incoming LO waits there before the hit, not killed.
PRE_HIT_LOW = round(FLAT - FLAT / 3, 6)
#: D14: from the hit the incoming LO rises to flat while the outgoing LO falls to PRE_HIT_LOW ("down to 2/3, where
#: incoming just was"), over the share of the body fade in which a linear fade loses its first third, never under
#: CROSSOVER_MIN_BARS; after it the outgoing LO keeps falling with the body fade. Both lows partly present is intended.
CROSSOVER_SHARE = 1 / 3
CROSSOVER_MIN_BARS = 2.0
#: How a gated blend hands the lows over (D14, the maintainer: "bass swap is one option and can sound great ... the bass-fade I
#: described is a safer default, but our goal is to have variance as well"). The crossover is the default; the swap
#: stays a first-class choice (bass_swap_blend): incoming lows killed until the hit, open ON it, the outgoing lows
#: edging down over SWAP_EDGE_BARS.
HANDOFF_CROSSOVER = "crossover"
HANDOFF_SWAP = "swap"
SWAP_EDGE_BARS = 1.0
#: D11: the incoming deck is heard, lows out, for at least this long before its bass may land.
MIN_INTRO_BARS = 2
#: Where a lane lands the hit when the incoming bass is already there, as a fraction of the bars (then clamped to
#: ``hit_bounds``): a crossfade gives way early and fades long, an overlap late, fade-in-fade-out halfway.
_LANE_HIT = {"crossfade": 0.25, "overlap": 0.75, "fade_in_fade_out": 0.5}
#: How a style's outgoing deck gives way relative to the incoming bass (D11).
HIT_ON_START = "start"  # a cut: the incoming enters on its bass, the outgoing goes on bar 0
HIT_GATED = "gated"  # a blend: nothing on the outgoing before the hit, the body fades from it to the handover
HIT_ON_HANDOVER = "handover"  # lull_then_drop: the outgoing holds to the handover, where the incoming drop lands
#: double_drop: both tracks build for 16 bars and land their drops together on the handover (no outgoing-lull gate).
HIT_ON_ALIGNED_DROP = "aligned_drop"
#: echo_out and noise_build: the incoming enters on its bass at the exit bar E, where the outgoing dry closes (echo
#: out) or the noise riser cuts on the drop (noise build).
HIT_ON_EXIT = "exit"
#: Spec 09-transitions-fx 4.3, mimicked from Spotify's "Echo 1/2 cut end": the send opens over the 2 beats before
#: the exit bar to ECHO_SEND, the dry closes on it, and the tail rings ECHO_TAIL_BARS before the handover.
ECHO_SEND = 0.7
ECHO_SEND_BARS = 0.5  # 2 beats
ECHO_TAIL_BARS = 1
#: #10246, (private path) section 2.4: NOISE's riser rides the level up across the build with
#: the filter opening, then cuts on the drop.
NOISE_LEVEL = 0.8
NOISE_SWEEP = 0.7
STYLE_TEMPO_MODES = ("sync_to_out", "none")


#: #10148, spec 04 decision 1: a blend glides to the incoming tempo from this gap (percent) up to Beat Sync's reach.
GLIDE_MIN_GAP_PCT = 0.5


def plan_tempo_mode(style: Style, bars: int, gap_pct: float | None, reach_pct: float) -> str:
    """The tempo mode a style's plan carries: its own on unsynced styles, cuts and 1-bar plans, glide_to_in on a blend
    whose gap is from GLIDE_MIN_GAP_PCT to the reach, else sync_to_out."""
    if style.tempo_mode != "sync_to_out" or bars <= 1 or not style.needs_beatmatch or gap_pct is None:
        return style.tempo_mode
    return "glide_to_in" if GLIDE_MIN_GAP_PCT <= gap_pct <= reach_pct else "sync_to_out"


def exit_bar(bars: int) -> float:
    """Echo out's exit bar E: the dry closes and the incoming starts here, one tail bar before the handover."""
    return float(bars - ECHO_TAIL_BARS)


def hit_bounds(bars: int, min_body: int) -> tuple[float, float]:
    """The plan bars a blend's bass hit may land on: after the intro, early enough for the whole body fade."""
    return float(MIN_INTRO_BARS), float(bars - min_body)


def default_hit_bar(volume: str, bars: int, min_body: int) -> float:
    """Where a blend lands the hit when the incoming bass is already there by then (its lows held out until it)."""
    low, high = hit_bounds(bars, min_body)
    return min(high, max(low, float(round(_LANE_HIT[volume] * bars))))


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
    #: How the outgoing deck gives way relative to the incoming bass hit (D11): HIT_ON_START, HIT_GATED or
    #: HIT_ON_HANDOVER.
    hit: str = HIT_GATED
    #: A hand-built blend's lane, for its default hit bar (a parametric style reads its params).
    lane: str = "overlap"
    #: The plan's tempo mode: "none" for the styles that do not beatmatch (cut, drop_swap, echo_out, noise_build).
    tempo_mode: str = "sync_to_out"

    def __post_init__(self) -> None:
        if self.tempo_mode not in STYLE_TEMPO_MODES:
            raise ValueError(f"style {self.name!r} tempo_mode must be one of {STYLE_TEMPO_MODES}, got {self.tempo_mode!r}")

    def clamp_bars(self, bars: int) -> int:
        return max(self.min_bars, min(self.max_bars, bars))

    @property
    def min_body_fade_bars(self) -> int:
        return DEFAULT_MIN_BODY_FADE_BARS if self.params is None else self.params.min_body_fade_bars

    @property
    def volume_lane(self) -> str:
        return self.lane if self.params is None else self.params.volume

    def build(self, bars: int, out_deck: int, in_deck: int, hit_bar: float | None = None) -> list[Event]:
        """The style's events over ``bars``; ``hit_bar`` is where the incoming bass lands (the proposer sets it from
        the incoming track's bass entry, D11), and a style refuses a hit it cannot honor."""
        if not self.available or self.builder is None:
            raise ValueError(f"style {self.name!r} is not available: {self.unavailable_reason}")
        clamped = self.clamp_bars(bars)
        if hit_bar is not None:
            if self.hit == HIT_ON_START and hit_bar != 0.0:
                raise ValueError(f"style {self.name!r} gives way on bar 0, so its incoming bass must land there")
            if self.hit == HIT_ON_HANDOVER and hit_bar != float(clamped):
                raise ValueError(f"style {self.name!r} lands the incoming drop on the handover, bar {clamped}")
            if self.hit == HIT_ON_ALIGNED_DROP and hit_bar != float(clamped):
                raise ValueError(
                    f"style {self.name!r} lands the aligned incoming drop on the handover, bar {clamped}"
                )
            if self.hit == HIT_ON_EXIT and hit_bar != exit_bar(clamped):
                raise ValueError(
                    f"style {self.name!r} lands the incoming bass on its exit bar, bar {exit_bar(clamped):g}"
                )
            if self.hit == HIT_GATED:
                low, high = hit_bounds(clamped, self.min_body_fade_bars)
                if not low <= hit_bar <= high:
                    raise ValueError(
                        f"bass hit at bar {hit_bar} is outside {low:g}..{high:g} for a {clamped}-bar {self.name}"
                        f" (intro {MIN_INTRO_BARS} bars, body fade {self.min_body_fade_bars})"
                    )
        return self.builder(clamped, out_deck, in_deck, hit_bar)

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
            "tempo_mode": self.tempo_mode,
        }


def _set(at_bar: float, deck: int | None, control: str, value: float) -> Event:
    return {"at_bar": at_bar, "deck": deck, "set": {control: value}}


def _ramp(at_bar: float, deck: int | None, control: str, value: float, over_bars: float) -> Event:
    return {"at_bar": at_bar, "deck": deck, "ramp": {control: value}, "over_bars": over_bars}


def _noise_set(at_bar: float, deck: int, level: float, sweep: float) -> Event:
    return {"at_bar": at_bar, "deck": deck, "set": {"fx_noise": {"level": level, "sweep": sweep}}}


def _noise_ramp(at_bar: float, deck: int, level: float, sweep: float, over_bars: float) -> Event:
    return {
        "at_bar": at_bar,
        "deck": deck,
        "ramp": {"fx_noise": {"level": level, "sweep": sweep}},
        "over_bars": over_bars,
    }


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


def handover_events(bars: int, out_deck: int, in_deck: int) -> list[Event]:
    """The handover every style ends on, for a plan built outside the catalog (a captured hand mix)."""
    return _handover(bars, out_deck, in_deck)


def _cut(bars: int, out_deck: int, in_deck: int, _hit_bar: float | None = None, /) -> list[Event]:
    return [
        _set(0, in_deck, "fader", 1.0),
        _set(0, in_deck, "eq_low", FLAT),
        _do(0, in_deck, "play"),
        _set(0, out_deck, "fader", KILL),
        *_handover(bars, out_deck, in_deck),
    ]


#: #10146, spec 05 section 3: the unsynced fallback offered when a grid is untrusted is this many bars at nominal BPM
#: (mimicked: Spotify Mix's short gridless fade).
FALLBACK_CROSSFADE_BARS = 2


def short_crossfade_events(out_deck: int, in_deck: int) -> list[Event]:
    """#10146: a 2-bar equal-power fader crossfade with the lows left flat, for a pair whose grid is not trusted."""
    bars = FALLBACK_CROSSFADE_BARS
    steps = [(k + 1) / CURVE_SEGMENTS for k in range(CURVE_SEGMENTS)]
    return [
        _set(0, in_deck, "fader", KILL),
        _do(0, in_deck, "play"),
        *_chain(in_deck, "fader", 0, bars, [_gain(DEFAULT_FADE_SHAPE, t, rising=True) for t in steps]),
        *_chain(out_deck, "fader", 0, bars, [_gain(DEFAULT_FADE_SHAPE, t, rising=False) for t in steps]),
        *_handover(bars, out_deck, in_deck),
    ]


def _echo_out(bars: int, out_deck: int, in_deck: int, _hit_bar: float | None = None, /) -> list[Event]:
    """Echo out (spec 09-transitions-fx 4.3): the send opens over the 2 beats before E, the dry closes on E as the
    incoming starts at full on its bass, and the tail rings one bar before the handover resets the effect.
    The outgoing fader is never moved: the echo return is pre-fader, so a fader pull would kill the tail."""
    exit_at = exit_bar(bars)
    return [
        _set(exit_at - ECHO_SEND_BARS, out_deck, "fx_echo", 0.0),
        _ramp(exit_at - ECHO_SEND_BARS, out_deck, "fx_echo", ECHO_SEND, ECHO_SEND_BARS),
        _set(exit_at, out_deck, "fx_dry", 0.0),
        _set(exit_at, in_deck, "fader", 1.0),
        _set(exit_at, in_deck, "eq_low", FLAT),
        _do(exit_at, in_deck, "play"),
        *_handover(bars, out_deck, in_deck),
        _do(bars, out_deck, "fx_reset"),
    ]


def _noise_build(bars: int, out_deck: int, in_deck: int, _hit_bar: float | None = None, /) -> list[Event]:
    """Noise build (#10246): the outgoing deck's NOISE level and sweep ride up over the build to the drop bar E, cut
    to 0 on its downbeat as the outgoing is cut and the incoming starts on its drop; the handover resets the effect."""
    exit_at = exit_bar(bars)
    return [
        _noise_set(0, out_deck, 0.0, 0.0),
        _noise_ramp(0, out_deck, NOISE_LEVEL, NOISE_SWEEP, exit_at),
        _noise_set(exit_at, out_deck, 0.0, 0.0),
        _set(exit_at, out_deck, "fader", KILL),
        _set(exit_at, in_deck, "fader", 1.0),
        _set(exit_at, in_deck, "eq_low", FLAT),
        _do(exit_at, in_deck, "play"),
        *_handover(bars, out_deck, in_deck),
        _do(bars, out_deck, "fx_reset"),
    ]


def _drop_swap(bars: int, out_deck: int, in_deck: int, _hit_bar: float | None = None, /) -> list[Event]:
    # The incoming window starts ON its drop; the outgoing deck is cut on the
    # same downbeat. Same mechanics as a cut, different window choice.
    return _cut(bars, out_deck, in_deck)


def _double_drop(bars: int, out_deck: int, in_deck: int, hit_bar: float | None = None, /) -> list[Event]:
    """Both decks build from plan bar 0; outgoing plays through until both drops smash on the handover."""
    if hit_bar is not None and hit_bar != float(bars):
        raise ValueError(f"double_drop lands both drops on the handover, bar {bars}, not {hit_bar}")
    return [
        _set(0, in_deck, "fader", 1.0),
        _set(0, in_deck, "eq_low", KILL),
        _do(0, in_deck, "play"),
        _set(bars, in_deck, "eq_low", FLAT),
        *_handover(bars, out_deck, in_deck),
    ]


@dataclass(frozen=True)
class StyleParams:
    """One point in the style space: the parameters a parametric style is built from."""

    volume: str
    fade_shape: str = "equal_power"
    swap_position: str | None = "center"
    filter: str | None = None
    stems: str | None = None
    #: D12: the outgoing body fades over at least this many bars after the hit.
    min_body_fade_bars: int = DEFAULT_MIN_BODY_FADE_BARS
    #: D11: the incoming fader before its bass hit, in dB (lows killed).
    pre_hit_db: float = DEFAULT_PRE_HIT_DB

    def __post_init__(self) -> None:
        if not -24.0 <= self.pre_hit_db <= 0.0:
            raise ValueError(f"pre_hit_db {self.pre_hit_db} is outside -24 to 0 dB")
        if not 1 <= self.min_body_fade_bars <= 64:
            raise ValueError(f"min_body_fade_bars {self.min_body_fade_bars} is outside 1 to 64")
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
            "min_body_fade_bars": self.min_body_fade_bars,
            "pre_hit_db": self.pre_hit_db,
        }


_DIPPED_OUT_END = 0.75
_DIPPED_IN_START = 0.25


def _dipped_pair(x: float) -> tuple[float, float]:
    """Nominal Melt pair on abstract progress ``x`` in [0, 1]: out on [0, 0.75], in on [0.25, 1].

    Equal-power within each leg; at ``x == 0.5`` both faders are ~0.5 and power ~0.5 (~-3 dB).
    """
    if x <= 0.0:
        f_out = 1.0
    elif x < _DIPPED_OUT_END:
        f_out = math.cos(math.pi / 2 * (x / _DIPPED_OUT_END))
    else:
        f_out = 0.0
    if x < _DIPPED_IN_START:
        f_in = 0.0
    elif x <= 1.0:
        f_in = math.sin(math.pi / 2 * ((x - _DIPPED_IN_START) / (1.0 - _DIPPED_IN_START)))
    else:
        f_in = 1.0
    return f_out, f_in


def _dipped_body_faders(t_body: float) -> tuple[float, float]:
    """Body progress ``t_body`` in [0, 1] after the hit: gated warp of the dipped law (AGENT-41).

    Mid-body keeps the spec dip (~0.5 / ~0.5); the outgoing leg stretches to silence at handover only.
    """
    x = 0.5 + t_body * 0.5
    f_out = 0.5 * math.cos(math.pi / 2 * t_body)
    f_in = _dipped_pair(x)[1]
    return f_out, f_in


def _smooth_ease(t: float) -> float:
    """Cubic S-curve progress 0..1: s = 3t^2 - 2t^3."""
    t = min(1.0, max(0.0, t))
    return t * t * (3.0 - 2.0 * t)


def _gain(shape: str, t: float, rising: bool) -> float:
    """Fader gain at fade progress ``t`` (0..1): equal power is sin/cos of a quarter turn, linear is ``t``."""
    if shape == "equal_power":
        return math.sin(t * math.pi / 2) if rising else math.cos(t * math.pi / 2)
    if shape == "linear":
        return t if rising else 1.0 - t
    if shape == "dipped":
        if rising:
            # Pre-hit ends at nominal x=0.5 with incoming at unity; the -3 dB dip is mid-body, not on the hit.
            x = _DIPPED_IN_START + t * (0.5 - _DIPPED_IN_START)
            return _dipped_pair(x)[1] / 0.5
        return _dipped_pair(t * _DIPPED_OUT_END)[0]
    if shape == "smooth":
        s = _smooth_ease(t)
        return math.sin(s * math.pi / 2) if rising else math.cos(s * math.pi / 2)
    raise ValueError(f"unknown fade shape {shape!r}")


def _chain(deck: int, control: str, start: float, span: float, values: list[float]) -> list[Event]:
    """A curve as a chain of linear ramps, one per value, each over an equal share of ``span``."""
    step = span / len(values)
    return [_ramp(start + k * step, deck, control, round(value, 6), step) for k, value in enumerate(values)]


def _body_fade(shape: str, pre: float) -> list[tuple[float, float]]:
    """(outgoing, incoming) fader values at each CURVE_SEGMENTS breakpoint of the body fade (D12).

    Equal power continues the quarter turn from where the incoming already is (sin theta0 = pre), so outgoing^2 +
    incoming^2 stays at one full deck: the mix never gains level over the louder solo, and the incoming never dips
    below its pre-hit level. Linear moves both straight, the incoming from its pre-hit level."""
    points = []
    for k in range(1, CURVE_SEGMENTS + 1):
        t = k / CURVE_SEGMENTS
        if shape == "equal_power":
            theta = math.asin(pre) + t * (math.pi / 2 - math.asin(pre))
            points.append((math.cos(theta), math.sin(theta)))
        elif shape == "linear":
            points.append((1.0 - t, pre + (1.0 - pre) * t))
        elif shape == "dipped":
            f_out, f_in = _dipped_body_faders(t)
            f_in = pre + (f_in - 0.5) * ((1.0 - pre) / 0.5)
            points.append((f_out, f_in))
        elif shape == "smooth":
            s = _smooth_ease(t)
            theta0 = math.asin(pre)
            theta = theta0 + s * (math.pi / 2 - theta0)
            points.append((math.cos(theta), math.sin(theta)))
        else:
            raise ValueError(f"unknown fade shape {shape!r}")
    return points


#: The EQ knob law in dB at the kill end (0.5 flat, 0 = -26 dB, linear in dB).
KNOB_FLOOR_DB = -26.0


#: r5: the low band's target sum, a little under one deck so the straight ramps between breakpoints stay inside it.
LOW_BAND_TARGET = 0.97


def knob_amp(knob: float) -> float:
    """Linear amplitude of a cut-only EQ knob (0..FLAT) on the law: 0.5 flat, 0 = -26 dB, linear in dB."""
    return 10.0 ** ((min(knob, FLAT) / FLAT - 1.0) * -KNOB_FLOOR_DB / 20.0)


def amp_knob(amp: float) -> float:
    """The knob (KILL..FLAT) that gives ``amp`` on the law; below the -26 dB floor it is KILL."""
    if amp >= 1.0:
        return FLAT
    db = 20.0 * math.log10(max(amp, 1e-9))
    return max(KILL, FLAT * (1.0 + db / -KNOB_FLOOR_DB))


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * min(1.0, max(0.0, t))


#: r6/r7: the power budget for the two decks' bodies after the hit: one deck (r6 tried +0.5 dB and clipped). The incoming
#: plays at unity from the switch, so the outgoing's mid and high EQ come down to keep inside it (p03: reductions are
#: band moves driven by what the incoming brings, not a fader ease); provisional, read against true_peak on r6.
BODY_POWER = 1.0
#: r7: the outgoing mids never go below -6 dB while both bodies play, so the outgoing stays audible (p10); past that the
#: incoming fader takes the trim.
OUT_BAND_FLOOR = 0.5
#: Round 20: the most the incoming fader gives up inside the crossover to keep the two lows at one deck in amplitude.
CROSSOVER_TRIM_DB = 2.0
CROSSOVER_TRIM_FLOOR = 10.0 ** (-CROSSOVER_TRIM_DB / 20.0)


def _out_fader_at(bar: float, span: float, edge: float, pre: float, shape: str) -> float:
    """The outgoing fader ``bar`` bars after the hit: held on the power curve for ``edge`` bars, then the long fade."""
    if bar <= edge:
        return body_start(shape, pre)
    t = (bar - edge) / max(span - edge, 1e-9)
    if shape == "equal_power":
        theta0 = math.asin(pre)
        return math.cos(theta0 + t * (math.pi / 2 - theta0))
    if shape == "dipped":
        return _dipped_body_faders(t)[0]
    if shape == "smooth":
        s = _smooth_ease(t)
        theta0 = math.asin(pre)
        return math.cos(theta0 + s * (math.pi / 2 - theta0))
    if shape == "linear":
        return 1.0 - t
    raise ValueError(f"unknown fade shape {shape!r}")


def _after_hit(
    spec_handoff: str, hit: float, span: float, pre: float, shape: str, out_deck: int, in_deck: int
) -> list[Event]:
    """Every deck move from the hit to the handover (D14, D15, r6), sampled on one grid and chained.

    Crossover: the incoming fader rises to unity by the switch while the two lows cross over equal-power in the band
    (the incoming LO from its -8.7 dB park, sin^2 + cos^2 = 1). Swap (p10): the incoming lands at unity with full lows
    ON the hit and the outgoing lows go; the outgoing fader holds for the swap edge and only then starts its fade, so
    the fade never muffles the hit. Both: the low band is held at one deck in amplitude (correlated kicks) and the
    outgoing mids and highs at BODY_POWER in power."""
    swap = spec_handoff == HANDOFF_SWAP
    cross = _switch_bars(spec_handoff, span)
    edge = cross if swap else 0.0
    # Two equal-step sections, so the switch itself is a breakpoint: hit to switch, switch to handover.
    tail = span - cross
    grid = [cross * k / (2 * CURVE_SEGMENTS) for k in range(2 * CURVE_SEGMENTS + 1)]
    if tail > 1e-9:
        grid += [cross + tail * k / (2 * CURVE_SEGMENTS) for k in range(1, 2 * CURVE_SEGMENTS + 1)]
    f_outs, f_ins, out_lows, in_lows, bands = [], [], [], [], []
    for bar in grid:
        t_body = bar / span if span > 1e-9 else 0.0
        if shape == "dipped":
            f_out = _dipped_body_faders(t_body)[0]
            if swap:
                f_in, in_amp, planned = 1.0, 1.0, 0.0
            else:
                cross_f_in, in_amp, planned = _crossover_at(bar, cross, tail, pre)
                if bar <= cross:
                    f_in = cross_f_in
                else:
                    f_in_spec = _dipped_body_faders(t_body)[1]
                    dipped_in = pre + (f_in_spec - 0.5) * ((1.0 - pre) / 0.5)
                    u = (bar - cross) / max(tail, 1e-9)
                    f_in = cross_f_in + (dipped_in - cross_f_in) * u
        else:
            f_out = _out_fader_at(bar, span, edge, pre, shape)
            f_in, in_amp, planned = (1.0, 1.0, 0.0) if swap else _crossover_at(bar, cross, tail, pre)
        band = _out_band(f_in, f_out)
        f_in = min(f_in, math.sqrt(max(0.0, BODY_POWER - (f_out * band) ** 2)))
        if not swap and bar <= cross:
            # Round 20: inside the crossover the maintainer's knob positions stand, and beatmatched kicks add in amplitude, so
            # the incoming fader is trimmed to keep out_low + in_low at one deck, by at most CROSSOVER_TRIM_DB.
            # Never under the pre-hit level: a quieter drop than its own lead-in is the dip D8 forbids.
            f_in = min(f_in, max(pre, f_in * CROSSOVER_TRIM_FLOOR, (1.0 - f_out * planned) / in_amp))
        room = (LOW_BAND_TARGET - f_in * in_amp) / f_out if f_out > 1e-6 else 0.0
        f_outs.append(f_out)
        f_ins.append(f_in)
        in_lows.append(amp_knob(in_amp))
        # The low-band room applies once the crossover is done; inside it the maintainer's knob positions stand (they can sum
        # up to about +2 dB in the lows for a bar or two, the cost of never leaving the bass uncarried).
        out_lows.append(amp_knob(planned) if not swap and bar <= cross else min(amp_knob(planned), amp_knob(room)))
        bands.append(amp_knob(band))
    events = [_set(hit, out_deck, "fader", round(f_outs[0], 6)), _set(hit, in_deck, "fader", round(f_ins[0], 6))]
    events += [_set(hit, out_deck, "eq_low", round(out_lows[0], 6)), _set(hit, in_deck, "eq_low", round(in_lows[0], 6))]
    events += [_set(hit, out_deck, "eq_mid", round(bands[0], 6))]
    lanes = (
        (out_deck, "fader", f_outs),
        (in_deck, "fader", f_ins),
        (out_deck, "eq_low", out_lows),
        (in_deck, "eq_low", in_lows),
        (out_deck, "eq_mid", bands),
    )
    return events + _chained_lanes(lanes, hit, cross, tail)


def _crossover_at(bar: float, cross: float, tail: float, pre: float) -> tuple[float, float, float]:
    """The incoming fader, the incoming low amplitude and the outgoing low amplitude asked for at ``bar``.

    the maintainer's rule, exactly (r7, p15): the incoming LO rises from its 1/3-down park to flat while the outgoing LO falls to
    that same 1/3 and HOLDS there until the incoming is flat; only then does it fall with the body fade. Never killed
    on the hit in a crossover."""
    u = min(1.0, bar / cross)
    past = (bar - cross) / max(tail, 1e-9)
    planned = knob_amp(_lerp(FLAT, PRE_HIT_LOW, u)) if bar <= cross else knob_amp(PRE_HIT_LOW) * (1.0 - past)
    return _lerp(pre, 1.0, u), knob_amp(_lerp(PRE_HIT_LOW, FLAT, u)), planned


def _out_band(f_in: float, f_out: float) -> float:
    """r7 headroom: the outgoing mids give way first, down to OUT_BAND_FLOOR; the rest comes off the incoming fader,
    so the two bodies stay within one deck in power and nothing clips by construction."""
    band = math.sqrt(max(0.0, BODY_POWER - f_in * f_in)) / f_out if f_out > 1e-6 else 1.0
    return min(1.0, max(OUT_BAND_FLOOR, band))


def _chained_lanes(
    lanes: tuple[tuple[int, str, list[float]], ...], hit: float, cross: float, tail: float
) -> list[Event]:
    """Each lane's samples as ramps: hit to switch, then switch to handover when there is a tail."""
    n = 2 * CURVE_SEGMENTS
    events: list[Event] = []
    for deck, control, values in lanes:
        events += _chain(deck, control, hit, cross, values[1 : n + 1])
        if tail > 1e-9:
            events += _chain(deck, control, hit + cross, tail, values[n + 1 :])
    return events


def _switch_bars(handoff: str, span: float) -> float:
    """Bars from the hit to the dominance switch: the crossover's end, or the swap edge."""
    return crossover_bars(span) if handoff == HANDOFF_CROSSOVER else min(SWAP_EDGE_BARS, span)


def crossover_bars(span: float) -> float:
    """D14: how long the two lows cross over after the hit, inside a body fade of ``span`` bars."""
    return min(span, max(CROSSOVER_MIN_BARS, CROSSOVER_SHARE * span))


def body_start(shape: str, pre: float) -> float:
    """The outgoing fader on the hit: equal power puts it where outgoing^2 + pre^2 = one deck; linear leaves it full."""
    if shape in {"equal_power", "smooth"}:
        return math.sqrt(1.0 - pre * pre)
    if shape == "linear":
        return 1.0
    if shape == "dipped":
        return math.sqrt(max(0.0, 0.5 - pre * pre))
    raise ValueError(f"unknown fade shape {shape!r}")


@dataclass(frozen=True)
class _Blend:
    """A gated blend (D11, D12): what it moves besides the faders and lows."""

    volume: str
    shape: str = DEFAULT_FADE_SHAPE
    min_body: int = DEFAULT_MIN_BODY_FADE_BARS
    pre_hit_db: float = DEFAULT_PRE_HIT_DB
    #: The incoming filter at bar 0, opened to flat by the hit.
    in_filter: float | None = None
    #: Where the outgoing filter sweeps to across the body fade.
    out_filter: float | None = None
    #: The incoming highs at bar 0, opened by the hit; the outgoing highs ease to it across the body fade.
    highs: float | None = None
    #: The incoming LO before the hit (D14): PRE_HIT_LOW, or KILL where the style's point is a killed entry.
    pre_hit_low: float = PRE_HIT_LOW
    #: HANDOFF_CROSSOVER (default) or HANDOFF_SWAP.
    handoff: str = HANDOFF_CROSSOVER

    def __post_init__(self) -> None:
        if self.handoff not in (HANDOFF_CROSSOVER, HANDOFF_SWAP):
            raise ValueError(f"handoff {self.handoff!r} is not one of crossover, swap")


def _gated_blend(spec: _Blend) -> Builder:
    """The one blend law (D11, D12): before the hit the outgoing deck is untouched and the incoming deck rises to its
    pre-hit level with its lows out; on the hit the incoming lows open and the outgoing lows edge down; from the hit to
    the handover the outgoing body fades with an equal-power law, so nothing dips before the hit and nothing sums above
    one full deck after it."""

    def build(bars: int, out_deck: int, in_deck: int, hit_bar: float | None = None, /) -> list[Event]:
        low, high = hit_bounds(bars, spec.min_body)
        hit = default_hit_bar(spec.volume, bars, spec.min_body) if hit_bar is None else float(hit_bar)
        if not low <= hit <= high:
            raise ValueError(f"bass hit at bar {hit} is outside {low:g}..{high:g} for a {bars}-bar blend")
        pre = 10.0 ** (spec.pre_hit_db / 20.0)
        rise = [pre * _gain(spec.shape, k / CURVE_SEGMENTS, True) for k in range(1, CURVE_SEGMENTS + 1)]
        span = bars - hit
        events = [
            _set(0, in_deck, "fader", KILL),
            _set(0, in_deck, "eq_low", spec.pre_hit_low),
            _do(0, in_deck, "play"),
            *_chain(in_deck, "fader", 0.0, hit, rise),
        ]
        if spec.in_filter is not None:
            events += [_set(0, in_deck, "filter", spec.in_filter), _ramp(0, in_deck, "filter", FLAT, hit)]
        if spec.highs is not None:
            events += [_set(0, in_deck, "eq_high", spec.highs), _ramp(0, in_deck, "eq_high", FLAT, hit)]
        # The hit: the two lows cross over (D14), the incoming rising to flat while the outgoing falls to where the
        # incoming just was, then the outgoing LO keeps falling with the body fade. The outgoing fader starts the body
        # fade ON the power curve (Codex #7818: chaining from 1.0 held the first segment above one full deck).
        events += [
            # r5 (smash the drop) and r6: the incoming at unity by the switch, the long late fade the outgoing's.
            *_after_hit(spec.handoff, hit, span, pre, spec.shape, out_deck, in_deck),
        ]
        if spec.out_filter is not None:
            events.append(_ramp(hit, out_deck, "filter", spec.out_filter, span))
        if spec.highs is not None:
            events.append(_ramp(hit, out_deck, "eq_high", spec.highs, span))
        return [*events, *_handover(bars, out_deck, in_deck)]

    return build


def _lull_then_drop(pre_hit_db: float) -> Builder:
    """D13: the outgoing deck idles in its own lull, a little of the incoming comes in under it with its lows out, and
    the incoming drop lands at full level on the handover (the smash). The outgoing deck gives way only there, where the
    incoming bass lands (D11), so this is the one style whose loudness dip before the hit is intended."""

    def build(bars: int, out_deck: int, in_deck: int, hit_bar: float | None = None, /) -> list[Event]:
        if hit_bar is not None and hit_bar != float(bars):
            raise ValueError(f"lull_then_drop lands the incoming drop on the handover, bar {bars}, not {hit_bar}")
        pre = 10.0 ** (pre_hit_db / 20.0)
        rise = [pre * _gain(DEFAULT_FADE_SHAPE, k / CURVE_SEGMENTS, True) for k in range(1, CURVE_SEGMENTS + 1)]
        return [
            _set(0, in_deck, "fader", KILL),
            _set(0, in_deck, "eq_low", KILL),
            _do(0, in_deck, "play"),
            *_chain(in_deck, "fader", 0.0, bars / 2, rise),
            *_handover(bars, out_deck, in_deck),
            # After the outgoing stop (same-bar events run in plan order): the drop at full level, lows in.
            _set(bars, in_deck, "fader", 1.0),
            _set(bars, in_deck, "eq_low", FLAT),
        ]

    return build


_FILTER_ENDS: dict[str, tuple[float | None, float | None]] = {
    "lp_in_hp_out": (LOWPASS_SWEEP, HIGHPASS_SWEEP),
    "lp_both": (LOWPASS_SWEEP, LOWPASS_SWEEP),
    "hp_both": (HIGHPASS_SWEEP, HIGHPASS_SWEEP),
    "hp_out": (None, HIGHPASS_SWEEP),
    "lp_out": (None, LOWPASS_SWEEP),
}


def parametric_builder(params: StyleParams) -> Builder:
    """The builder for one point in the style space (AGENT-34). Bars are the plan's own clock (bar 0 = out window)."""
    if params.stems is not None:

        def refuse(*_args: Any) -> list[Event]:
            raise ValueError("stem moves are not part of plan execution yet")

        return refuse
    if params.swap_position == "end":
        return _lull_then_drop(params.pre_hit_db)
    in_filter, out_filter = (None, None) if params.filter is None else _FILTER_ENDS[params.filter]
    # Both LP and HP incoming entries keep lows killed until the hit: an LP still passes bass (D14).
    killed_entry = in_filter is not None
    return _gated_blend(
        _Blend(
            volume=params.volume,
            shape=params.fade_shape,
            min_body=params.min_body_fade_bars,
            pre_hit_db=params.pre_hit_db,
            in_filter=in_filter,
            out_filter=out_filter,
            pre_hit_low=KILL if killed_entry else PRE_HIT_LOW,
        )
    )


def _three_band_builder(params: StyleParams) -> Builder:
    """Blend's lows-first choreography, on downbeats of the post-hit body (#10164).

    Effective band amplitudes include the fader. Factoring the high fade between
    its EQ and fader leaves room for the mid fade without asking either EQ to boost.
    The issue's low/mid/high order takes precedence over research spec 02's order.
    """

    def build(bars: int, out_deck: int, in_deck: int, hit_bar: float | None = None, /) -> list[Event]:
        hit = default_hit_bar(params.volume, bars, params.min_body_fade_bars) if hit_bar is None else hit_bar
        body = bars - hit
        low_center, mid_center, high_center = (math.floor(hit + body * f + 0.5) for f in (0.25, 0.5, 0.75))
        low_start, low_end = low_center - 0.5, low_center + 0.5
        mid_start, mid_end = mid_center - body / 4, mid_center + body / 4
        high_start = 2 * high_center - bars
        low_deadline = low_end + 1.0
        windows = ((low_start, low_end), (mid_start, mid_end), (high_start, float(bars)))
        # A shared grid includes every boundary and center. Knobs interpolate in dB,
        # so dense segments preserve effective power between the analytic samples.
        grid = {hit, low_deadline, float(bars)}
        for start, end in (*windows, (low_end, low_deadline)):
            grid.update(start + (end - start) * k / (2 * CURVE_SEGMENTS) for k in range(2 * CURVE_SEGMENTS + 1))
        times = sorted(grid)
        pre = 10.0 ** (params.pre_hit_db / 20.0)
        park, floor = knob_amp(PRE_HIT_LOW), knob_amp(KILL)
        equal = math.sqrt(0.5)

        def band_pair(bar: float, start: float, end: float) -> tuple[float, float]:
            u = min(1.0, max(0.0, (bar - start) / (end - start)))
            return math.cos(u * math.pi / 2), math.sin(u * math.pi / 2)

        def values(bar: float) -> tuple[float, ...]:
            in_fader = _lerp(pre, 1.0, (bar - low_start) / (low_center - low_start))
            high_out, high_in = band_pair(bar, high_start, bars)
            out_high_amp = max(floor, math.sqrt(high_out))
            out_fader = high_out / out_high_amp if bar < bars else 0.0
            if bar <= low_center:
                u = (bar - low_start) / (low_center - low_start)
                low_out, low_in = _lerp(1.0, equal, u), _lerp(pre * park, equal, u)
            elif bar <= low_end:
                u = (bar - low_center) / (low_end - low_center)
                low_out, low_in = _lerp(equal, park, u), _lerp(equal, 1.0, u)
            else:
                low_out, low_in = 0.0, 1.0
            out_low_knob = (
                amp_knob(low_out)
                if bar <= low_end
                else _lerp(PRE_HIT_LOW, KILL, (bar - low_end) / (low_deadline - low_end))
            )
            mid_out, mid_in = band_pair(bar, mid_start, mid_end)
            # Once killed, hold the raw knob rather than lifting it to repair a
            # falling fader. Handle the terminal zero explicitly.
            out_mid_knob = amp_knob(mid_out / out_fader) if bar < mid_end else KILL
            return (
                out_fader,
                in_fader,
                out_low_knob,
                amp_knob(low_in / in_fader),
                out_mid_knob,
                amp_knob(mid_in / in_fader),
                amp_knob(out_high_amp),
                amp_knob(high_in / in_fader),
            )

        lanes = (
            (out_deck, "fader"),
            (in_deck, "fader"),
            (out_deck, "eq_low"),
            (in_deck, "eq_low"),
            (out_deck, "eq_mid"),
            (in_deck, "eq_mid"),
            (out_deck, "eq_high"),
            (in_deck, "eq_high"),
        )
        events = [
            _set(0, in_deck, "fader", KILL),
            _set(0, in_deck, "eq_low", PRE_HIT_LOW),
            _set(0, in_deck, "eq_mid", KILL),
            _set(0, in_deck, "eq_high", KILL),
            _do(0, in_deck, "play"),
        ]
        events += _chain(
            in_deck,
            "fader",
            0.0,
            hit,
            [pre * _gain(params.fade_shape, k / CURVE_SEGMENTS, True) for k in range(1, CURVE_SEGMENTS + 1)],
        )
        samples = [values(bar) for bar in times]
        for lane, (deck, control) in enumerate(lanes):
            events.append(_set(hit, deck, control, round(samples[0][lane], 6)))
            events.extend(
                _ramp(start, deck, control, round(sample[lane], 6), end - start)
                for start, end, sample in zip(times[:-1], times[1:], samples[1:], strict=True)
            )
        return events + _handover(bars, out_deck, in_deck)

    return build


def _parametric(
    name: str,
    summary: str,
    when: str,
    params: StyleParams,
    spotify: str | None,
    bars: tuple[int, int, int],
    *,
    builder: Builder | None = None,
    extra_controls: tuple[str, ...] = (),
) -> Style:
    controls = ["fader", "play", "master", "eq_low"]
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
        controls=tuple(controls) + (("stem_gain",) if params.stems else ()) + extra_controls,
        when=when,
        available=unavailable is None,
        unavailable_reason=unavailable,
        builder=parametric_builder(params) if builder is None else builder,
        params=params,
        spotify=spotify,
        hit=HIT_ON_HANDOVER if params.swap_position == "end" else HIT_GATED,
    )


#: The shortest gated blend: the intro before the hit plus the whole body fade after it (D11, D12).
_MIN_BLEND_BARS = MIN_INTRO_BARS + DEFAULT_MIN_BODY_FADE_BARS + 2

_STYLES: tuple[Style, ...] = (
    Style(
        name="cut",
        summary="Hard cut on a downbeat: the incoming deck starts on its bass as the outgoing one is muted (Spotify Slam).",
        min_bars=1,
        max_bars=1,
        default_bars=1,
        needs_beatmatch=False,
        controls=("fader", "play", "master"),
        when="Big key or tempo gaps, showy moments, or when the outgoing track ends abruptly.",
        builder=_cut,
        spotify="Slam",
        hit=HIT_ON_START,
        tempo_mode="none",
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
        hit=HIT_ON_START,
        tempo_mode="none",
    ),
    Style(
        name="double_drop",
        summary=(
            "Both tracks run 16-bar builds from plan bar 0 and land their drops together on the handover"
            " (DnB double drop)."
        ),
        min_bars=16,
        max_bars=16,
        default_bars=16,
        needs_beatmatch=True,
        controls=("fader", "play", "master", "eq_low"),
        when="Both tracks are 160-180 BPM with identifiable drops and 16 bars of run-up before each.",
        builder=_double_drop,
        hit=HIT_ON_ALIGNED_DROP,
    ),
    Style(
        name="filter_sweep",
        summary="The incoming track comes in under the outgoing one, which highpasses away over the body fade after the"
        " incoming bass lands.",
        min_bars=_MIN_BLEND_BARS,
        max_bars=32,
        default_bars=16,
        needs_beatmatch=True,
        controls=("filter", "fader", "eq_low", "play", "master"),
        when="Energetic transitions; hides a small key clash.",
        # Its point is a killed entry: the incoming lows stay out until the hit (D14 keeps KILL here).
        builder=_gated_blend(_Blend(volume="crossfade", out_filter=HIGHPASS_SWEEP, pre_hit_low=KILL)),
        lane="crossfade",
    ),
    Style(
        name="bass_swap_blend",
        summary="Beatmatched blend with the incoming lows held out until its bass lands, then one bassline at a time.",
        min_bars=_MIN_BLEND_BARS,
        max_bars=32,
        default_bars=16,
        needs_beatmatch=True,
        controls=("fader", "eq_low", "play", "master"),
        when="The default for compatible tracks: one bassline at a time, 16 to 32 bars.",
        # The hard swap, a first-class choice beside the default crossover (D14): lows killed until the hit, then
        # exchanged ON it.
        builder=_gated_blend(_Blend(volume="overlap", pre_hit_low=KILL, handoff=HANDOFF_SWAP)),
        lane="overlap",
    ),
    Style(
        name="long_blend",
        summary="A long blend: highs eased in, lows in on the incoming bass, the outgoing track fades out after it.",
        min_bars=32,
        max_bars=64,
        default_bars=32,
        needs_beatmatch=True,
        controls=("fader", "eq_low", "eq_high", "play", "master"),
        when="Smooth or winding-down sets with compatible keys and close tempos.",
        builder=_gated_blend(_Blend(volume="crossfade", highs=EASED_HIGHS)),
        lane="crossfade",
    ),
    _parametric(
        "crossfade",
        "Equal-power crossfade of the outgoing body from the incoming bass hit (Spotify Fade).",
        "Compatible tracks when you want the smoothest possible handover.",
        StyleParams(volume="crossfade", fade_shape="smooth"),
        "Fade",
        (_MIN_BLEND_BARS, 32, 16),
    ),
    _parametric(
        "rise",
        "The incoming deck low-passed in, the outgoing high-passed out after the incoming bass lands (Spotify Rise,"
        " with the swap gated on the bass hit, D11).",
        "Building energy into the next track.",
        StyleParams(volume="overlap", filter="lp_in_hp_out"),
        "Rise",
        (_MIN_BLEND_BARS, 32, 16),
    ),
    _parametric(
        "wave",
        "Overlap with both decks low-passed (in opening, out closing after the bass hit) (Spotify Wave).",
        "A dark, rolling handover between deep or melodic tracks.",
        StyleParams(volume="overlap", filter="lp_both"),
        "Wave",
        (_MIN_BLEND_BARS, 32, 16),
    ),
    _parametric(
        "melt",
        "Fade in then fade out, both decks high-passed (the outgoing after the bass hit), the incoming lows killed until"
        " it (a filtered entry) (Spotify Melt).",
        "Airy handovers; thins both tracks so their lows never meet.",
        StyleParams(volume="fade_in_fade_out", filter="hp_both", fade_shape="dipped"),
        "Melt",
        (_MIN_BLEND_BARS, 32, 16),
    ),
    _parametric(
        "lull_then_drop",
        "The outgoing track idles in its own breakdown, a little of the incoming comes in under it, and the incoming"
        " drop lands at full level on the handover (D13).",
        "The outgoing track has a lull and the incoming one a drop within reach: introduce it, then smash its drop.",
        StyleParams(volume="overlap", swap_position="end", pre_hit_db=LULL_PRE_HIT_DB),
        None,
        (8, 32, 16),
    ),
    _parametric(
        "bass_phrase_swap",
        "Equal-power overlap with the bass hit on the incoming 4-bar boundary at phrase entry (spec 02 phrase).",
        "When the incoming bass should land on a phrase downbeat, not the blend center.",
        StyleParams(volume="overlap", swap_position="phrase"),
        None,
        (_MIN_BLEND_BARS, 32, 16),
    ),
    Style(
        name="echo_out",
        summary=(
            "Echo the outgoing track out: its echo send opens over the 2 beats before the exit bar, its dry closes"
            " on it as the incoming track starts on its bass, and the tail rings for a bar (Spotify Echo 1/2 cut end)."
        ),
        min_bars=2,
        max_bars=5,
        default_bars=3,
        needs_beatmatch=False,
        controls=("fx_echo", "fx_dry", "play", "master"),
        when="The universal escape for bad pairs: a tempo gap Beat Sync cannot reach, a key clash, two vocals.",
        builder=_echo_out,
        spotify="Echo 1/2 cut end",
        hit=HIT_ON_EXIT,
        tempo_mode="none",
    ),
    Style(
        name="noise_build",
        summary=(
            "A NOISE riser over the outgoing build: its level and sweep ride up to the drop bar, cut on the drop"
            " downbeat as the incoming track starts on its drop."
        ),
        min_bars=2,
        max_bars=8,
        default_bars=5,
        needs_beatmatch=False,
        controls=("fx_noise",),
        when="A build-into-drop handover between tracks that do not need to blend, for example a tempo gap.",
        builder=_noise_build,
        hit=HIT_ON_EXIT,
        tempo_mode="none",
    ),
    _parametric(
        "stem_swap",
        "Swap vocals between decks with stems over a crossfade.",
        "Acapella over the next beat; avoiding a vocal clash.",
        StyleParams(volume="crossfade", stems="vocals_swap"),
        None,
        (_MIN_BLEND_BARS, 32, 16),
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
    _parametric(
        "three_band_fade",
        "Overlap with lows, mids and highs crossing on successive body downbeats (Spotify Blend).",
        "Compatible beatmatched tracks with room for a gradual three-band handover.",
        StyleParams(volume="overlap"),
        "Blend",
        (12, 32, 16),
        builder=_three_band_builder(StyleParams(volume="overlap")),
        extra_controls=("eq_mid", "eq_high"),
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
