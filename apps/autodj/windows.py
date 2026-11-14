"""Ranked mix-in and mix-out windows (``get_mix_windows``).

Rule-based, after the canon doc's "Anatomy of a good transition" and Mixxx's
intro/outro cue model: leave and enter on phrase starts, prefer outros and
breakdowns going out and intros and builds coming in, keep vocals apart, and
know how many bars a window allows before the next vocal or drop. A learned
detector (CUE-DETR, Zehren et al.) can replace the scoring later without
changing this shape.

A window is a phrase start plus the most bars a blend can use from there:

* ``out``: from the window start to the end of the grid, so a long blend can
  run through the outro into the tail.
* ``in``: from the window start to the incoming track's next peak (drop or
  chorus) or first vocal bar, whichever comes first, so the incoming energy
  can land when the blend ends. Unpinned windows under 16 bars (64 beats)
  score zero but remain available for cut/drop-swap transitions.

A user's transition-in cue (AGENT-51, ADR-0128) overrides the rules for
``in``: when the track has one, it is the only in window offered. That is
the mimicked behavior of Traktor (Cruise mode mixes in at the Fade In cue)
and Mixxx (AutoDJ starts the incoming track at its intro start cue), which
take the user's placement as the answer rather than one candidate among many.

Rule-based mix-in candidates also need mean ``bar_energy`` over the next four
bars at least 0.4 of the track peak (Zehren salience), except the user's
transition-in cue.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Literal

from apps.autodj.profile import VOCAL_BAR_THRESHOLD, TrackProfile
from apps.autodj.sections import (
    BREAKDOWN,
    BRIDGE,
    BUILD,
    INSTRUMENTAL,
    INTRO,
    MIX_IN_LABELS,
    MIX_OUT_LABELS,
    OUTRO,
    PEAK_LABELS,
)

Role = Literal["out", "in"]
ROLES: tuple[Role, ...] = ("out", "in")

#: The longest blend any style asks for (``long_blend``), in bars.
MAX_WINDOW_BARS = 64
#: How many fallback bars to offer when a track has no phrase analysis.
FALLBACK_BARS = 32
#: Fewer bars than this on an outro out window is flagged for later refusal (#11180).
MIN_BLEND_BARS = 4
#: Incoming forward mixability for blends: four beats per bar, 64 beats (#11851).
MIN_INCOMING_MIXABLE_BARS = 16
#: Mix-in salience (Zehren 2020): mean energy over this many bars after the window start.
SALIENCE_BARS = 4
#: Minimum fraction of the track peak that salience window must reach.
SALIENCE_MIN_RATIO = 0.4
#: Bittner 2017 A position prior: the last fraction of A a mix-out may start in.
OUT_POSITION_PRIOR = 0.25
#: Bittner 2017 B position prior: the first fraction of B a mix-in may start in.
IN_POSITION_PRIOR = 0.20

_OUT_LABEL_WEIGHT = {OUTRO: 1.0, BREAKDOWN: 0.8, BRIDGE: 0.7, INSTRUMENTAL: 0.7}
_IN_LABEL_WEIGHT = {INTRO: 1.0, BUILD: 0.8, INSTRUMENTAL: 0.7}
#: Above every rule score (weighted sum of terms in 0..1 times a band prior in 0..1),
#: so a caller that merges windows from elsewhere still ranks the user's cue first.
USER_CUE_SCORE = 2.0
_OTHER_LABEL_WEIGHT = 0.3
#: Weight of the MIK ``fall`` term on out windows (spec 17 section 5c; first guess, mimicked snap rule).
FALL_WEIGHT = 0.15
#: A MIK step up of at least this many levels at an in window's end is a rise landing.
RISE_MIN_STEP = 2


#: #12485: in-window evidence, in fallback priority order (Mixxx AutoDJ section 2.2).
IN_SOURCES = ("transition_in_cue", "phrase_cue", "analyzed_section", "time_fallback")


@dataclass(frozen=True)
class MixWindow:
    """One place to leave (``out``) or enter (``in``) a track."""

    role: Role
    start_bar: int
    max_bars: int
    label: str
    energy: float | None
    vocal_free_bars: int | None
    peak_bar: int | None
    score: float
    reason: str
    pinned: bool = False
    #: Outro tail shorter than 4 bars on the grid (issue #11180); later blends may refuse.
    too_short: bool = False
    #: #10144: the grid trust tier over this window's span (BAR, BEAT, NONE) and why when not BAR.
    grid_tier: str = "BAR"
    grid_reason: str | None = None
    #: #12485: the evidence an in window came from (one of IN_SOURCES); empty on out windows.
    source: str = ""

    def summary(self) -> dict[str, Any]:
        doc = {
            "role": self.role,
            "start_bar": self.start_bar,
            "max_bars": self.max_bars,
            "label": self.label,
            "energy": None if self.energy is None else round(self.energy, 2),
            "vocal_free_bars": self.vocal_free_bars,
            "peak_bar": self.peak_bar,
            "score": round(self.score, 3),
            "reason": self.reason,
            "too_short": self.too_short,
        }
        if self.source:
            doc["source"] = self.source
        if self.grid_tier != "BAR":
            doc |= {"grid_tier": self.grid_tier, "grid_reason": self.grid_reason}
        return doc


def incoming_mixability_reason(window: MixWindow) -> str | None:
    """Why an incoming window lacks blend capacity; pinned cues bypass this floor."""
    if window.pinned or window.max_bars >= MIN_INCOMING_MIXABLE_BARS:
        return None
    return (
        f"in-window forward mixability short: max_bars={window.max_bars}, "
        f"requires {MIN_INCOMING_MIXABLE_BARS} bars (64 beats)"
    )


def _first_vocal_bar(profile: TrackProfile, start_bar: int) -> int | None:
    if profile.bar_vocals is None:
        return None
    for index in range(max(0, start_bar - 1), len(profile.bar_vocals)):
        if profile.bar_vocals[index] >= VOCAL_BAR_THRESHOLD:
            return index + 1
    return None


def _next_peak_bar(profile: TrackProfile, start_bar: int) -> int | None:
    for section in profile.sections:
        if section.start_bar > start_bar and section.label in PEAK_LABELS:
            return section.start_bar
    return None


def _vocal_free_run(profile: TrackProfile, start_bar: int, bars: int) -> int | None:
    """Bars from ``start_bar`` before the first vocal bar, capped at ``bars``."""
    if profile.bar_vocals is None:
        return None
    first = _first_vocal_bar(profile, start_bar)
    if first is None:
        return bars
    return max(0, min(bars, first - start_bar))


def _out_position_prior(bars: int, start_bar: int) -> float:
    """Linear ramp: 1.0 in the last ``OUT_POSITION_PRIOR`` of the grid, down toward bar 1."""
    if bars <= 0:
        return 1.0
    bars_eff = max(1, bars)
    band_start = (1.0 - OUT_POSITION_PRIOR) * bars_eff
    return min(1.0, start_bar / max(1.0, band_start))


def _in_position_prior(bars: int, start_bar: int) -> float:
    """Linear ramp: 1.0 in the first ``IN_POSITION_PRIOR`` of the grid, down toward the tail."""
    if bars <= 0:
        return 1.0
    bars_eff = max(1, bars)
    band_end = IN_POSITION_PRIOR * bars_eff
    if start_bar <= band_end:
        return 1.0
    return max(0.0, (bars_eff - start_bar) / (bars_eff - band_end))


def _mik_step_at(profile: TrackProfile, bar: int) -> tuple[int, int] | None:
    """``(level_before, level_after)`` of the snapped MIK boundary at ``bar``, if any."""
    if profile.mik_bar_level is None:
        return None
    for boundary in profile.mik_boundaries:
        if boundary.snapped_bar == bar:
            return boundary.level_before, boundary.level_after
    return None


def _offer_fall_windows(profile: TrackProfile, offer: Callable[[MixWindow], None]) -> None:
    tail_from = max(1, int(profile.bars * 0.5))
    outro = next(
        (s.start_bar for s in profile.sections if s.label == OUTRO and s.start_bar >= tail_from), profile.bars + 1
    )
    falls: list[MixWindow] = []
    for boundary in profile.mik_boundaries if profile.mik_bar_level is not None else ():
        bar = boundary.snapped_bar
        drop = boundary.level_before - boundary.level_after
        if bar is None or drop < 1 or not tail_from <= bar < outro:
            continue
        section = profile.section_at(bar)
        label = section.label if section is not None else "unknown"
        why = f"energy falls {boundary.level_before} to {boundary.level_after} at bar {bar}"
        window = _out_window(profile, bar, label, why)
        if window is not None:
            falls.append(replace(window, score=window.score + FALL_WEIGHT * min(1.0, drop / 3.0)))
    if falls:
        last = max(falls, key=lambda window: window.start_bar)
        top = max(window.score for window in falls)
        if last.score < top:
            falls[falls.index(last)] = replace(last, score=top + 0.001)
    for window in falls:
        offer(window)


def _tier_fields(profile: TrackProfile, start_bar: int, bars: int) -> dict[str, Any]:
    tier = profile.grid_tier(start_bar, bars)
    return {"grid_tier": tier.tier, "grid_reason": tier.reason}


def _out_window(profile: TrackProfile, start_bar: int, label: str, why: str) -> MixWindow | None:
    last_bar = profile.bars  # the last downbeat on the grid
    # The blend must be over BY the last downbeat: the bar after it is the
    # track's tail, and a deck that runs out mid-plan cannot hand over. A start on
    # the last downbeat leaves no bar to blend in, so there is no window to offer.
    if last_bar - start_bar < 1:
        return None
    max_bars = min(MAX_WINDOW_BARS, last_bar - start_bar)
    vocal_free = _vocal_free_run(profile, start_bar, max_bars)
    label_weight = _OUT_LABEL_WEIGHT.get(label, _OTHER_LABEL_WEIGHT)
    position = start_bar / max(1, last_bar)  # later is better going out
    vocal_term = 1.0 if vocal_free is None else vocal_free / max_bars
    length_term = min(1.0, max_bars / 32.0)
    score = 0.45 * label_weight + 0.2 * vocal_term + 0.2 * position + 0.15 * length_term
    score *= _out_position_prior(last_bar, start_bar)
    reasons = [why, f"{max_bars} bars to the last downbeat"]
    if vocal_free is not None:
        reasons.append(f"{vocal_free} vocal-free bars")
    return MixWindow(
        role="out",
        start_bar=start_bar,
        max_bars=max_bars,
        label=label,
        energy=profile.mean_between(profile.bar_energy, start_bar, max_bars),
        vocal_free_bars=vocal_free,
        peak_bar=None,
        score=score,
        reason="; ".join(reasons),
        too_short=label == OUTRO and max_bars < MIN_BLEND_BARS,
        **_tier_fields(profile, start_bar, max_bars),
    )


def _in_window(profile: TrackProfile, start_bar: int, label: str, why: str, *, pinned: bool = False) -> MixWindow:
    peak = _next_peak_bar(profile, start_bar)
    first_vocal = _first_vocal_bar(profile, start_bar)
    limit = profile.bars - start_bar + 1
    stops = [bar for bar in (peak, first_vocal) if bar is not None and bar > start_bar]
    if stops:
        limit = min(limit, min(stops) - start_bar)
    max_bars = max(1, min(MAX_WINDOW_BARS, limit))
    vocal_free = _vocal_free_run(profile, start_bar, max_bars)
    label_weight = _IN_LABEL_WEIGHT.get(label, _OTHER_LABEL_WEIGHT)
    position = 1.0 - (start_bar - 1) / max(1, profile.bars)  # earlier is better coming in
    vocal_term = 1.0 if vocal_free is None else vocal_free / max_bars
    length_term = min(1.0, max_bars / 16.0)
    landing = 1.0 if peak is not None else 0.0
    step = _mik_step_at(profile, start_bar + max_bars)
    rise = step is not None and step[1] - step[0] >= RISE_MIN_STEP
    if rise:
        landing = 1.0
    score = 0.4 * label_weight + 0.15 * vocal_term + 0.15 * position + 0.15 * length_term + 0.15 * landing
    score *= _in_position_prior(profile.bars, start_bar)
    reasons = [why, f"{max_bars} usable bars"]
    if peak is not None:
        reasons.append(f"lands on a peak at bar {peak}")
    if rise and step is not None:
        reasons.append(f"lands on an energy rise {step[0]} to {step[1]} at bar {start_bar + max_bars}")
    if first_vocal is not None and first_vocal >= start_bar:
        reasons.append(f"first vocal at bar {first_vocal}")
    window = MixWindow(
        role="in",
        start_bar=start_bar,
        max_bars=max_bars,
        label=label,
        energy=profile.mean_between(profile.bar_energy, start_bar, max_bars),
        vocal_free_bars=vocal_free,
        peak_bar=peak,
        score=USER_CUE_SCORE if pinned else score,
        reason="; ".join(reasons),
        pinned=pinned,
        **_tier_fields(profile, start_bar, max_bars),
    )
    window = replace(window, source=_in_source(profile, start_bar, pinned))
    refusal = incoming_mixability_reason(window)
    if refusal is not None:
        return replace(window, score=0.0, reason=f"{window.reason}; {refusal}")
    return window


def _in_source(profile: TrackProfile, start_bar: int, pinned: bool) -> str:
    """Which evidence an in window at ``start_bar`` rests on (#12485)."""
    if pinned:
        return "transition_in_cue"
    if profile.section_at(start_bar) is None:
        return "time_fallback"
    return "phrase_cue" if profile.sections_source == "rekordbox" else "analyzed_section"


def _offer_out_windows(
    profile: TrackProfile, candidates: dict[int, MixWindow], offer: Callable[[MixWindow], None]
) -> None:
    tail_from = max(1, int(profile.bars * 0.5))
    for section in profile.sections:
        if section.label in MIX_OUT_LABELS:
            _offer_if_open(
                offer, _out_window(profile, section.start_bar, section.label, f"{section.label} starts here")
            )
        elif section.start_bar >= tail_from:
            _offer_if_open(
                offer, _out_window(profile, section.start_bar, section.label, "phrase start in the back half")
            )
    _offer_fall_windows(profile, offer)
    fallback = max(1, profile.bars - FALLBACK_BARS + 1)
    fallback -= (fallback - 1) % 8  # keep it on an 8-bar boundary from bar 1
    if not candidates:
        _offer_if_open(offer, _out_window(profile, fallback, "unknown", "no phrase analysis: last 32 bars"))


def _offer_if_open(offer: Callable[[MixWindow], None], window: MixWindow | None) -> None:
    if window is not None:
        offer(window)


def _offer_in_windows(
    profile: TrackProfile, candidates: dict[int, MixWindow], offer: Callable[[MixWindow], None]
) -> None:
    for section in profile.sections:
        if section.label in MIX_IN_LABELS:
            offer(_in_window(profile, section.start_bar, section.label, f"{section.label} starts here"))
    first = profile.sections[0] if profile.sections else None
    if first is not None and first.start_bar not in candidates:
        offer(_in_window(profile, first.start_bar, first.label, "the first phrase"))
    if not candidates:
        offer(_in_window(profile, 1, "unknown", "no phrase analysis: from bar 1"))


def _salience_gate(profile: TrackProfile, start_bar: int) -> tuple[bool, str]:
    series = profile.bar_energy
    if series is None:
        return True, "salience skipped: energy series missing"
    peak = max(series)
    if peak <= 0:
        return True, "salience skipped: track peak is zero"
    heard = profile.mean_between(series, start_bar, SALIENCE_BARS)
    if heard is None:
        return False, f"salience below {SALIENCE_MIN_RATIO} of the track peak over {SALIENCE_BARS} bars"
    ratio = heard / peak
    if ratio >= SALIENCE_MIN_RATIO:
        return True, f"salience {ratio:.2f} of the track peak over {SALIENCE_BARS} bars"
    return False, f"salience {ratio:.2f} below {SALIENCE_MIN_RATIO} of the track peak over {SALIENCE_BARS} bars"


def _transition_in_window(profile: TrackProfile, bar: int) -> MixWindow:
    """The in window at the user's transition-in cue.

    The cue is stored on a downbeat, so ``bar`` is always a valid window
    start. It need NOT be a phrase start: a cue that lands mid-phrase (say
    bar 13 of an intro that starts at bar 9) still opens the window there,
    labelled with the phrase it falls inside, because where the user put it
    is the point. Its length is still capped at the next peak or first vocal
    bar like any in window, so a cue placed late in an intro gets a short
    blend rather than one that runs over the drop.
    """
    section = profile.section_at(bar)
    label = section.label if section is not None else "unknown"
    on_phrase = section is not None and section.start_bar == bar
    why = "your transition-in cue" + ("" if on_phrase or section is None else f" (mid-{label}, not a phrase start)")
    return _in_window(profile, bar, label, why, pinned=True)


def mix_windows(
    profile: TrackProfile, role: Role, *, limit: int = 5, min_start_bar: float | None = None
) -> list[MixWindow]:
    """Ranked windows for ``role``, best first, at most ``limit``.

    For ``in``, a track with a transition-in cue returns that one window.
    Other in-windows under 16 bars score zero, retained for cut/drop-swap use.
    When ``min_start_bar`` is set, windows with ``start_bar <= min_start_bar`` are dropped before the cap.
    """
    if role not in ROLES:
        raise ValueError(f"role must be 'out' or 'in', got {role!r}")
    if role == "in" and profile.transition_in_bar is not None:
        return [_transition_in_window(profile, profile.transition_in_bar)]
    candidates: dict[int, MixWindow] = {}

    def offer(window: MixWindow) -> None:
        current = candidates.get(window.start_bar)
        if current is None or window.score > current.score:
            candidates[window.start_bar] = window

    if role == "out":
        _offer_out_windows(profile, candidates, offer)
    else:
        _offer_in_windows(profile, candidates, offer)
        for start_bar, window in list(candidates.items()):
            kept, note = _salience_gate(profile, start_bar)
            candidates[start_bar] = replace(
                window,
                score=window.score if kept else 0.0,
                reason=f"{window.reason}; {note}",
            )
    ranked = sorted(candidates.values(), key=lambda window: (-window.score, window.start_bar))
    if min_start_bar is not None:
        ranked = [window for window in ranked if window.start_bar > min_start_bar]
    return ranked[: max(1, limit)]


def window_at(profile: TrackProfile, role: Role, start_bar: int) -> MixWindow:
    """The window starting at a bar the caller chose (ranked or not)."""
    if role not in ROLES:
        raise ValueError(f"role must be 'out' or 'in', got {role!r}")
    if not 1 <= start_bar <= profile.bars:
        raise ValueError(f"start_bar {start_bar} is outside the grid (bars 1..{profile.bars})")
    section = profile.section_at(start_bar)
    label = section.label if section is not None else "unknown"
    if role == "out":
        window = _out_window(profile, start_bar, label, "the bar you named")
        if window is None:
            raise ValueError(f"start_bar {start_bar} is the last downbeat: no bar is left to blend into")
        return window
    return _in_window(profile, start_bar, label, "the bar you named")
