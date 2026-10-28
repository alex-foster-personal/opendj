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

from apps.autodj.plan import TransitionPlan, Validation, first_dispatch_bar, live_mapping, parse_plan, validate_plan
from apps.autodj.profile import TrackProfile, match_trim_db, trim_db_of_knob
from apps.autodj.scoring import PairScore, rank_key, score_windows
from apps.autodj.sections import PEAK_LABELS
from apps.autodj.styles import STYLES, Style
from apps.autodj.windows import MixWindow, mix_windows

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
PARAM_STYLES: tuple[str, ...] = ("crossfade", "rise", "wave", "melt")

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
    ),
    WIND_DOWN: (
        ("long_blend", 64),
        ("long_blend", 32),
        ("bass_swap_blend", 32),
        ("crossfade", 32),
        ("melt", 16),
        ("wave", 16),
        ("rise", 16),
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
        return 0.0, "offline proposal: no live outgoing trim, so the match assumes it at unity"
    if "mixer" not in live:
        return 0.0, "the live mirror carries no mixer, so the match assumes the outgoing trim at unity"
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
        raise ValueError(f"deck {out_deck} live trim {knob!r} is not a knob value")  # noqa: TRY004 - one ValueError for every corrupt mirror value
    return trim_db_of_knob(float(knob)), None


def _present(level: Mapping[str, Any], key: str, where: str) -> Any:
    if key not in level:
        raise ValueError(f"{where} has no {key!r}: a partial mirror is refused, never read as unity")
    return level[key]


def propose_transition(
    out_profile: TrackProfile,
    in_profile: TrackProfile,
    *,
    out_deck: int,
    in_deck: int,
    intent: str | None = None,
    min_out_bar: float | None = None,
    live: dict[str, Any] | None = None,
    top: int | None = 3,
) -> dict[str, Any]:
    """The ``top`` best complete plans, each validated, with one-line rationales."""
    parsed = parse_intent(intent)
    out_windows = [
        window
        for window in mix_windows(out_profile, "out", limit=6)
        if min_out_bar is None or window.start_bar > min_out_bar
    ]
    proposals: list[Proposal] = []
    seen: set[tuple[str, int, int, int]] = set()
    out_trim_db, out_trim_note = _outgoing_trim_db(live, out_deck)
    trim_db, trim_reason = match_trim_db(out_profile, in_profile, out_trim_db)
    if out_trim_note is not None and trim_db is not None:
        trim_reason = f"{trim_reason}; {out_trim_note}" if trim_reason else out_trim_note
    for rank, (style_name, wanted_bars) in enumerate(_PREFERENCES[parsed]):
        style = STYLES[style_name]
        if not style.available:
            continue
        for out_window in out_windows[:3]:
            for in_window in _in_windows_for(parsed, in_profile, style)[:3]:
                bars = _overlap_bars(style, wanted_bars, out_window, in_window, out_profile.bars)
                if bars is None:
                    continue
                key = (style_name, bars, out_window.start_bar, in_window.start_bar)
                if key in seen:
                    continue
                seen.add(key)
                pair = score_windows(out_profile, in_profile, out_window, in_window, overlap_bars=bars)
                rationale = _rationale(style_name, style, bars, out_window, in_window)
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
                            "window": {"start_bar": in_window.start_bar, "bars": bars},
                            "trim_db": trim_db,
                            "trim_reason": trim_reason,
                        },
                        "style": style_name,
                        "tempo": {"mode": "sync_to_out"},
                        "events": style.build(bars, out_deck, in_deck),
                        "intent": intent or parsed,
                        "rationale": rationale,
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
    }
