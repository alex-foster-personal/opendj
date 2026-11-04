"""The deterministic chooser (``propose_transition``).

No model: given two profiles, an optional intent and (live) where the outgoing
deck is now, it tries every available style over the best window pairs,
builds each plan, validates it, and ranks by window/pair score minus what
validation found. It is what an agent calls first, and what AutoPlay can use
to climb to rung 3 with no LLM at all (canon doc, "Works without an LLM").

Intents follow the canon doc's "From a prompt to parameters" table. Free text
is matched by keywords, and the parsed intent is returned so the caller can
see what was understood instead of trusting a silent guess.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from apps.autodj.notation import print_score
from apps.autodj.plan import (
    SHAPE_STYLES,
    TransitionPlan,
    Validation,
    first_dispatch_bar,
    live_mapping,
    pair_beat_sync_fold,
    parse_plan,
    validate_plan,
)
from apps.autodj.profile import DECK_LOAD_TRIM_DB, Section, TrackProfile, match_trim_db, trim_db_of_knob
from apps.autodj.scoring import PairScore, rank_key, score_windows
from apps.autodj.sections import BREAKDOWN, BRIDGE, PEAK_LABELS
from apps.autodj.styles import (
    HIT_GATED,
    HIT_ON_HANDOVER,
    HIT_ON_START,
    STYLES,
    Style,
    default_hit_bar,
    hit_bounds,
)
from apps.autodj.windows import MixWindow, mix_windows, window_at

SMOOTH = "smooth"
ENERGY_UP = "energy_up"
WIND_DOWN = "wind_down"
SHOWY = "showy"
VOCALS_CLEAN = "vocals_clean"
INTENTS: tuple[str, ...] = (SMOOTH, ENERGY_UP, WIND_DOWN, SHOWY, VOCALS_CLEAN)

_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (VOCALS_CLEAN, ("vocal", "vocals", "acapella", "lyrics")),
    (WIND_DOWN, ("wind down", "chill", "calm", "down", "slow", "slower", "mellow", "cool")),
    (ENERGY_UP, ("energy", "hype", "peak", "bang", "faster", "harder", "up")),
    (SHOWY, ("showy", "surprise", "creative", "trick", "flashy", "wild")),
    (SMOOTH, ("smooth", "seamless", "nobody", "subtle", "blend")),
)

#: The parameter styles (AGENT-34): every intent offers all four, after its older styles so the preference bonus of
#: those does not move; score_pair weights are untouched.
PARAM_STYLES: tuple[str, ...] = ("crossfade", "rise", "wave", "melt", "lull_then_drop")

#: D13: the shape a style's plans carry, from the plan module's own map.
SHAPE_OF_STYLE: dict[str, str] = {style_name: shape for shape, style_name in SHAPE_STYLES.items()}

#: Styles each intent tries, in order of preference, and the bars it prefers.
_PREFERENCES: dict[str, tuple[tuple[str, int], ...]] = {
    SMOOTH: (
        ("long_blend", 32),
        ("bass_swap_blend", 32),
        ("bass_swap_blend", 16),
        ("filter_sweep", 8),
        ("crossfade", 16),
        ("wave", 16),
        ("melt", 16),
        ("rise", 16),
        ("lull_then_drop", 16),
    ),
    ENERGY_UP: (
        ("bass_swap_blend", 16),
        ("drop_swap", 1),
        ("filter_sweep", 8),
        ("cut", 1),
        ("rise", 16),
        ("wave", 16),
        ("crossfade", 8),
        ("melt", 8),
        ("lull_then_drop", 16),
    ),
    WIND_DOWN: (
        ("long_blend", 64),
        ("long_blend", 32),
        ("bass_swap_blend", 32),
        ("crossfade", 32),
        ("melt", 16),
        ("wave", 16),
        ("rise", 16),
        ("lull_then_drop", 16),
    ),
    SHOWY: (
        ("drop_swap", 1),
        ("cut", 1),
        ("filter_sweep", 8),
        ("bass_swap_blend", 16),
        ("rise", 16),
        ("wave", 16),
        ("melt", 8),
        ("crossfade", 8),
        ("lull_then_drop", 16),
    ),
    VOCALS_CLEAN: (
        ("bass_swap_blend", 16),
        ("filter_sweep", 8),
        ("cut", 1),
        ("long_blend", 32),
        ("melt", 16),
        ("crossfade", 16),
        ("wave", 16),
        ("rise", 16),
        ("lull_then_drop", 16),
    ),
}


def _preference_bonus(intent: str, rank: int) -> float:
    """The older styles keep the bonus they had before the parameter styles were appended, counted against their own
    list (Sol P1 #7595: counting the whole list moved every one of them up by 0.24); the parameter styles, which come
    after, get none."""
    older = next(i for i, (name, _bars) in enumerate(_PREFERENCES[intent]) if name in PARAM_STYLES)
    return 0.06 * (older - rank) if rank < older else 0.0


def parse_intent(text: str | None) -> str:
    """One of :data:`INTENTS` from free text; ``smooth`` when nothing matches."""
    if not text:
        return SMOOTH
    lowered = text.strip().lower()
    if lowered in INTENTS:
        return lowered
    for intent, words in _KEYWORDS:
        if any(re.search(rf"\b{re.escape(word)}\b", lowered) for word in words):
            return intent
    return SMOOTH


@dataclass(frozen=True)
class Proposal:
    plan: TransitionPlan
    validation: Validation
    pair: PairScore
    score: float
    rationale: str

    def summary(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 3),
            "style": self.plan.style,
            "bars": self.plan.bars,
            "rationale": self.rationale,
            "plan": self.plan.to_json(),
            # AGENT-5858: the same plan as a mix score, for people and for agents.
            "notation": print_score(self.plan),
            "validation": self.validation.to_json(),
            "pair": self.pair.summary(),
        }


def _in_windows_for(intent: str, profile: TrackProfile, style: Style) -> list[MixWindow]:
    windows = mix_windows(profile, "in", limit=5)
    if style.name == "drop_swap":
        # Enter ON a drop: the window starts at a peak section.
        peaks = [s for s in profile.sections if s.label in PEAK_LABELS]
        return [
            MixWindow(
                role="in",
                start_bar=section.start_bar,
                max_bars=section.bars,
                label=section.label,
                energy=section.energy,
                vocal_free_bars=None,
                peak_bar=section.start_bar,
                score=0.8,
                reason=f"{section.label} starts here: enter on it",
            )
            for section in peaks[:3]
        ]
    if intent == ENERGY_UP:
        windows.sort(key=lambda window: (window.peak_bar is None, -window.score))
    return windows


def _penalty(validation: Validation, intent: str) -> float:
    if not validation.ok:
        return 1.0
    metrics = validation.metrics
    penalty = 0.04 * len(validation.warnings)
    vocal = metrics.get("vocal_overlap_bars") or 0
    bass = metrics.get("bass_overlap_bars") or 0
    penalty += (0.05 if intent == VOCALS_CLEAN else 0.02) * vocal
    penalty += 0.01 * bass
    return penalty


def _style_fit(style: Style, pair: PairScore) -> float:
    """Short or beat-free styles for clashing pairs, long blends for good ones."""
    fit = 0.0
    if style.needs_beatmatch and pair.beatmatchable is False and style.max_bars > 1:
        fit -= 0.4
    if pair.key_distance is not None and pair.key_distance > 2 and style.default_bars >= 16:
        fit -= 0.2
    if pair.vocal_clash_bars and style.default_bars >= 16:
        fit -= 0.1
    return fit


def _overlap_bars(
    style: Style, wanted_bars: int, out_window: MixWindow, in_window: MixWindow, grid_bars: int
) -> int | None:
    """The overlap a style gets in these windows, or None when the pair is not proposed: a window shorter than the style's
    minimum (clamping up to it would blend past the window the scoring and validation see), or a blend that runs past the
    last bar of the grid."""
    if out_window.max_bars < style.min_bars or (style.max_bars > 1 and in_window.max_bars < style.min_bars):
        return None
    bars = style.clamp_bars(min(wanted_bars, out_window.max_bars))
    if style.max_bars > 1:
        bars = style.clamp_bars(min(bars, in_window.max_bars))
    if out_window.start_bar + bars > grid_bars:
        return None
    return bars


#: D11: the incoming bass has landed at the first bar from which its bar_bass holds at BASS_ENTRY_FRACTION of the
#: track's own p90 for BASS_ENTRY_HOLD_BARS bars (a steady bassline, not one loud bar). Tequila's intro sits at 0.5
#: for 15 bars, then 0.95 on its drop.
BASS_ENTRY_FRACTION = 0.8
BASS_ENTRY_PERCENTILE = 0.9
BASS_ENTRY_HOLD_BARS = 4
#: D13: lull_then_drop is offered only when the incoming drop lies within this many bars of its in-window start.
LULL_REACH_BARS = 32
#: D13: an outgoing window is a lull when it is a breakdown or bridge in its own structure, or this quiet (section
#: energy is normalized within the track).
LULL_LABELS = frozenset({BREAKDOWN, BRIDGE})
LULL_ENERGY = 0.45


def incoming_bass_entry(profile: TrackProfile, from_bar: int) -> int | None:
    """The first incoming bar at or after ``from_bar`` where its steady bass starts (D11), or None when it never does
    (also when the profile has no bar_bass series: callers tell the two apart)."""
    series = profile.bar_bass
    if not series:
        return None
    reference = sorted(series)[min(len(series) - 1, int(BASS_ENTRY_PERCENTILE * len(series)))]
    threshold = BASS_ENTRY_FRACTION * reference
    for bar in range(max(1, from_bar), len(series) - BASS_ENTRY_HOLD_BARS + 2):
        if all(value >= threshold for value in series[bar - 1 : bar - 1 + BASS_ENTRY_HOLD_BARS]):
            return bar
    return None


_ENTRY_RULE = f"first bar with bar_bass >= {BASS_ENTRY_FRACTION} x the track's p90 for {BASS_ENTRY_HOLD_BARS} bars"


@dataclass(frozen=True)
class HitPlacement:
    """Where a style's outgoing deck may give way for this window pair (D11), or why the pair is not offered."""

    #: The incoming window's start bar: the scored one, or moved so its bass lands where the style needs it.
    in_start_bar: int
    #: The plan bar the incoming bass lands on, passed to ``Style.build``.
    hit_bar: float
    #: The plan's ``bass_entry`` document.
    entry: dict[str, Any] | None
    #: Rationale note, empty when the window is used as scored.
    note: str
    #: Why the pair is not offered, or None.
    refused: str | None = None


def _plan_bar(entry: int, in_start: int, fold: float) -> float:
    """Where the incoming bar ``entry`` starts on the plan clock (fold incoming bars per plan bar); fractional under a
    folded Beat Sync (Codex #7809: rounding up to a whole plan bar held the lows out through the drop's first bar)."""
    return max(0.0, (entry - in_start) / fold)


def is_lull(window: MixWindow) -> bool:
    return window.label in LULL_LABELS or (window.energy is not None and window.energy <= LULL_ENERGY)


def _lull_section(section: Section) -> bool:
    return section.label in LULL_LABELS or (section.energy is not None and section.energy <= LULL_ENERGY)


def lull_bars(profile: TrackProfile, start_bar: int) -> int:
    """How long the outgoing track stays in a lull from ``start_bar`` (D13): contiguous breakdown, bridge or quiet
    sections, so lull_then_drop never runs into the outgoing's own next drop (Codex #7818). 0 when not in one."""
    section = profile.section_at(start_bar)
    if section is None or not _lull_section(section):
        return 0
    end = section.end_bar
    while (following := profile.section_at(end)) is not None and _lull_section(following):
        end = following.end_bar
    return end - start_bar


def _drop_on_handover(
    in_profile: TrackProfile, entry: int, in_start: int, bars: int, fold: float
) -> tuple[int, float, int] | str:
    """D13: the in-window start that lands the incoming drop exactly on the handover, or why it cannot."""
    if entry - in_start > LULL_REACH_BARS:
        return f"the incoming drop (its bar {entry}) is not within {LULL_REACH_BARS} bars of bar {in_start}"
    if not float(bars * fold).is_integer():
        # Codex #7818: under a folded Beat Sync an overlap whose incoming span is fractional cannot put the drop
        # ON the handover; flooring it would start the drop a plan bar before the smash.
        return f"a {bars}-bar overlap cannot land the incoming drop on the handover under a {fold:g} fold"
    start = entry - int(bars * fold)
    if start < 1 or incoming_bass_entry(in_profile, start) != entry:
        return f"no bass-free {bars}-bar run-up before the incoming drop at its bar {entry}"
    return start, float(bars), entry


def _start_and_hit(
    style: Style, in_profile: TrackProfile, entry: int, in_start: int, bars: int, fold: float
) -> tuple[int, float, int] | str:
    """(in-window start, hit bar, entry used) for a known entry, or why this style cannot land it."""
    if style.hit == HIT_ON_START:
        return entry, 0.0, entry  # a cut enters ON the incoming bass
    if style.hit == HIT_ON_HANDOVER:
        return _drop_on_handover(in_profile, entry, in_start, bars, fold)
    if style.hit == HIT_GATED:
        low, high = hit_bounds(bars, style.min_body_fade_bars)
        start = in_start if _plan_bar(entry, in_start, fold) <= high else entry - math.floor(high * fold)
        hit = max(default_hit_bar(style.volume_lane, bars, style.min_body_fade_bars), _plan_bar(entry, start, fold))
        if not low <= hit <= high:
            return f"the incoming bass (its bar {entry}) cannot land between plan bars {low:g} and {high:g}"
        # A hit held past the entry must still find the bass playing there (Codex #7818: a short early run that has
        # ended would open the incoming lows into a bass-light stretch, the very hole the gate prevents).
        at_hit = start + math.floor(hit * fold)
        if hit > _plan_bar(entry, start, fold) and incoming_bass_entry(in_profile, at_hit) != at_hit:
            later = incoming_bass_entry(in_profile, at_hit)
            if later is None:
                return f"the incoming bass from its bar {entry} is gone by the hit (its bar {at_hit}) and never returns"
            return _start_and_hit(style, in_profile, later, start, bars, fold)
        return start, hit, entry
    raise ValueError(f"style {style.name!r} has an unknown hit kind {style.hit!r}")


def _unknown_entry(style: Style, in_start: int, bars: int) -> HitPlacement:
    """No bar_bass series: the style's own hit bar, said in the plan; a lull's drop cannot be found at all."""
    if style.hit == HIT_ON_HANDOVER:
        return HitPlacement(
            in_start, 0.0, None, "", "the incoming track has no bar_bass series, so its drop cannot be found"
        )
    hit = 0.0 if style.hit == HIT_ON_START else default_hit_bar(style.volume_lane, bars, style.min_body_fade_bars)
    unknown = "bass entry unknown: the incoming track has no bar_bass series; the style's own hit bar is used"
    entry = {"in_bar": None, "entry_plan_bar": None, "hit_plan_bar": hit, "note": unknown}
    return HitPlacement(in_start, hit, entry, f"; {unknown}")


def _fit_lull(style: Style, out_profile: TrackProfile, out_start: int, bars: int, fold: float) -> int | str:
    """D13: lull_then_drop's overlap inside the outgoing lull, at a fold-compatible length so the incoming drop lands
    exactly on the handover (Codex #7818), or why it cannot fit."""
    lull = lull_bars(out_profile, out_start)
    if lull < style.min_bars:
        return f"the outgoing lull at bar {out_start} lasts {lull} bars, under {style.min_bars}"
    fitted = min(bars, lull)
    while not float(fitted * fold).is_integer() and fitted > style.min_bars:
        fitted -= 1
    return fitted


def place_hit(
    style: Style, out_window: MixWindow, in_profile: TrackProfile, in_start: int, bars: int, fold: float = 1.0
) -> HitPlacement:
    """D11 for every style: nothing on the outgoing deck before the incoming bass has landed.

    ``fold`` is the pair's Beat Sync fold (``pair_beat_sync_fold``, incoming grid bars per plan bar, as ``layout_of``
    renders it). A cut must start the incoming on its bass, a blend lands the hit between its intro and its body fade,
    and lull_then_drop lands the incoming drop on the handover. When the scored in-window cannot do that, the window is
    moved to the bass entry, or the pair is refused with the reason; a missing bass series is said, never guessed."""
    if style.hit == HIT_ON_HANDOVER and not is_lull(out_window):
        reason = f"the outgoing window at bar {out_window.start_bar} ({out_window.label}) is not a lull"
        return HitPlacement(in_start, 0.0, None, "", reason)
    if not in_profile.bar_bass:
        return _unknown_entry(style, in_start, bars)
    entry = incoming_bass_entry(in_profile, in_start)
    if entry is None:
        return HitPlacement(in_start, 0.0, None, "", f"the incoming bass never lands after its bar {in_start}")
    placed = _start_and_hit(style, in_profile, entry, in_start, bars, fold)
    if isinstance(placed, str):
        return HitPlacement(in_start, 0.0, None, "", placed)
    start, hit, entry = placed
    if start + math.ceil(bars * fold) > in_profile.bars + 1:
        reason = f"moving the in-window to bar {start} for its bass runs past the incoming track's end"
        return HitPlacement(in_start, 0.0, None, "", reason)
    if start != in_start:
        note = f"; in-window moved from bar {in_start} to {start} so its bass (bar {entry}) lands on plan bar {hit:g}"
    elif _plan_bar(entry, start, fold) < hit:
        note = f"; incoming lows held out until plan bar {hit:g} (its bass runs from its bar {entry})"
    else:
        note = f"; incoming bass lands on plan bar {hit:g} (its bar {entry})"
    entry_doc = {
        "in_bar": entry,
        "entry_plan_bar": _plan_bar(entry, start, fold),
        "hit_plan_bar": hit,
        "note": _ENTRY_RULE,
    }
    return HitPlacement(start, hit, entry_doc, note)


def _rationale(style_name: str, style: Style, bars: int, out_window: MixWindow, in_window: MixWindow) -> str:
    rationale = (
        f"{style_name} over {bars} bars: leave at bar {out_window.start_bar} "
        f"({out_window.label}), enter at bar {in_window.start_bar} ({in_window.label})"
    )
    if in_window.peak_bar is not None and style.max_bars > 1:
        rationale += f"; its peak lands at bar {in_window.peak_bar}"
    return rationale


def _best_valid(proposals: list[Proposal], top: int | None) -> list[Proposal]:
    """The top proposals by score among those that validate: one that fails would be refused by schedule_plan. With
    ``top=None`` every valid one, ranked, so a caller that revalidates later can still fill its own top (Sol #7595)."""
    # AGENT-33: more measured pair terms first, then the score (scoring.rank_key).
    valid = sorted(
        (proposal for proposal in proposals if proposal.validation.ok),
        key=lambda proposal: rank_key(proposal.pair, proposal.score),
        reverse=True,
    )
    return valid if top is None else valid[: max(1, top)]


def _outgoing_trim_db(live: Mapping[str, Any] | None, out_deck: int) -> tuple[float, str | None]:
    """The outgoing channel's live trim in dB, with why it is unity when the mirror carries none (Sol P1 #7594).

    No live mirror (an offline proposal) or no ``mixer`` in it: no live trim, 0 dB, and the reason says so. A mixer
    that IS there is read strictly through ``live_mapping``, the one mirror reader: its channels, the outgoing channel and its trim must all
    be present and well formed, or this raises. Nothing is defaulted to unity from a partial or malformed mirror."""
    if live is None:
        return DECK_LOAD_TRIM_DB, (
            f"offline proposal: no live outgoing trim, so the match assumes the deck loaded at {DECK_LOAD_TRIM_DB:+.0f} dB"
        )
    if "mixer" not in live:
        return DECK_LOAD_TRIM_DB, (
            f"the live mirror carries no mixer, so the match assumes the outgoing trim at {DECK_LOAD_TRIM_DB:+.0f} dB"
        )
    mixer = live_mapping(live["mixer"], "live mirror mixer")
    channels = live_mapping(_present(mixer, "channels", "live mirror mixer"), "live mirror channels")
    channel = live_mapping(_present(channels, str(out_deck), "live mirror channels"), "live mirror channel")
    knob = _present(channel, "trim", f"deck {out_deck}'s live channel")
    if (
        isinstance(knob, bool)
        or not isinstance(knob, (int, float))
        or not math.isfinite(knob)
        or not 0.0 <= knob <= 1.0
    ):
        raise ValueError(f"deck {out_deck} live trim {knob!r} is not a knob value")
    return trim_db_of_knob(float(knob)), None


def _present(level: Mapping[str, Any], key: str, where: str) -> Any:
    if key not in level:
        raise ValueError(f"{where} has no {key!r}: a partial mirror is refused, never read as unity")
    return level[key]


#: AGENT-50: how many outgoing phrase starts nearest the chosen bar are tried, nearest first.
AROUND_WINDOWS = 2


def _windows_around(profile: TrackProfile, around_bar: float, min_out_bar: float | None) -> list[MixWindow]:
    """The outgoing windows at the phrase starts nearest ``around_bar``, past ``min_out_bar``; a start the window
    reader refuses (too close to the end) is skipped, never stretched."""
    starts = sorted(
        (s.start_bar for s in profile.sections if min_out_bar is None or s.start_bar > min_out_bar),
        key=lambda bar: (abs(bar - around_bar), bar),
    )
    windows: list[MixWindow] = []
    for start in starts:
        try:
            windows.append(window_at(profile, "out", start))
        except ValueError:
            continue
        if len(windows) == AROUND_WINDOWS:
            break
    return windows


def propose_transition(  # noqa: PLR0913 - keyword-only options of one entry point
    out_profile: TrackProfile,
    in_profile: TrackProfile,
    *,
    out_deck: int,
    in_deck: int,
    intent: str | None = None,
    min_out_bar: float | None = None,
    live: dict[str, Any] | None = None,
    top: int | None = 3,
    around_bar: float | None = None,
) -> dict[str, Any]:
    """The ``top`` best complete plans, each validated, with one-line rationales.

    ``around_bar`` (AGENT-50, "transition out around here"): leave from the outgoing phrase starts nearest that bar,
    whatever their window score, instead of the best-scored windows anywhere in the track."""
    parsed = parse_intent(intent)
    out_windows = (
        _windows_around(out_profile, around_bar, min_out_bar)
        if around_bar is not None
        else [
            window
            for window in mix_windows(out_profile, "out", limit=6)
            if min_out_bar is None or window.start_bar > min_out_bar
        ]
    )
    proposals: list[Proposal] = []
    not_offered: list[dict[str, Any]] = []
    seen: set[tuple[str, int, int, int]] = set()
    out_trim_db, out_trim_note = _outgoing_trim_db(live, out_deck)
    trim_db, trim_reason = match_trim_db(out_profile, in_profile, out_trim_db)
    if out_trim_note is not None and trim_db is not None:
        trim_reason = f"{trim_reason}; {out_trim_note}" if trim_reason else out_trim_note
    # Every proposal is tempo sync_to_out, so the incoming deck plays this many grid bars per plan bar (Sentry #7809).
    fold = pair_beat_sync_fold(out_profile.bpm, in_profile.bpm)
    for rank, (style_name, wanted_bars) in enumerate(_PREFERENCES[parsed]):
        style = STYLES[style_name]
        if not style.available:
            continue
        for out_window in out_windows[:3]:
            for in_window in _in_windows_for(parsed, in_profile, style)[:3]:
                bars = _overlap_bars(style, wanted_bars, out_window, in_window, out_profile.bars)
                if bars is None:
                    continue
                if style.hit == HIT_ON_HANDOVER:
                    fitted = _fit_lull(style, out_profile, out_window.start_bar, bars, fold)
                    if isinstance(fitted, str):
                        not_offered.append(
                            {
                                "style": style_name,
                                "bars": bars,
                                "out_bar": out_window.start_bar,
                                "in_bar": in_window.start_bar,
                                "reason": fitted,
                            }
                        )
                        continue
                    bars = fitted
                hit = place_hit(style, out_window, in_profile, in_window.start_bar, bars, fold)
                pinned_moved = (
                    in_window.pinned
                    and hit.refused is None
                    and hit.in_start_bar != in_window.start_bar
                )
                # Sentry #7818: two scored windows can move to one bass entry; dedupe on the window actually entered.
                entered_bar = in_window.start_bar if (hit.refused is not None or pinned_moved) else hit.in_start_bar
                key = (style_name, bars, out_window.start_bar, entered_bar)
                if key in seen:
                    continue
                seen.add(key)
                if hit.refused is not None or pinned_moved:
                    not_offered.append(
                        {
                            "style": style_name,
                            "bars": bars,
                            "out_bar": out_window.start_bar,
                            "in_bar": in_window.start_bar,
                            "reason": hit.refused
                            if hit.refused is not None
                            else (
                                f"in-window pinned at the transition-in cue (bar {in_window.start_bar}); "
                                f"place_hit would move it to bar {hit.in_start_bar}"
                            ),
                        }
                    )
                    continue
                # The window the plan actually enters on: the scored one, or moved to the incoming bass (D11) and re-read
                # there (Codex #7818: never the scored window's label, energy and score on another section).
                entered = (
                    in_window
                    if hit.in_start_bar == in_window.start_bar
                    else window_at(in_profile, "in", hit.in_start_bar)
                )
                pair = score_windows(out_profile, in_profile, out_window, entered, overlap_bars=bars)
                rationale = _rationale(style_name, style, bars, out_window, entered) + hit.note
                plan = parse_plan(
                    {
                        "out": {
                            "deck": out_deck,
                            "track": out_profile.stable_id,
                            "window": {"start_bar": out_window.start_bar, "bars": bars},
                        },
                        "in": {
                            "deck": in_deck,
                            "track": in_profile.stable_id,
                            "window": {"start_bar": entered.start_bar, "bars": bars},
                            "trim_db": trim_db,
                            "trim_reason": trim_reason,
                        },
                        "style": style_name,
                        "tempo": {"mode": "sync_to_out"},
                        "events": style.build(bars, out_deck, in_deck, hit.hit_bar),
                        "intent": intent or parsed,
                        "rationale": rationale,
                        "bass_entry": hit.entry,
                        "shape": SHAPE_OF_STYLE.get(style_name),
                    }
                )
                # Codex #7595: the window filter above sees only the start bar; a style's pre-roll (a compensated
                # blend's bar -1 ease) puts its first event earlier, so the whole plan must start after the live bar.
                if min_out_bar is not None and out_window.start_bar + first_dispatch_bar(plan) <= min_out_bar:
                    continue
                validation = validate_plan(plan, out_profile, in_profile, live=live)
                preference = _preference_bonus(parsed, rank)
                score = pair.score + preference + _style_fit(style, pair) - _penalty(validation, parsed)
                proposals.append(Proposal(plan, validation, pair, score, rationale))
    best = _best_valid(proposals, top)
    return {
        "intent": parsed,
        "intent_text": intent,
        "out_track": out_profile.stable_id,
        "in_track": in_profile.stable_id,
        "proposals": [proposal.summary() for proposal in best],
        "considered": len(proposals),
        # D11: window pairs a style could not use, and why (never a silent skip).
        "not_offered": not_offered,
    }
