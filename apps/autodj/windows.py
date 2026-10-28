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
  can land when the blend ends.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
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

_OUT_LABEL_WEIGHT = {OUTRO: 1.0, BREAKDOWN: 0.8, BRIDGE: 0.7, INSTRUMENTAL: 0.7}
_IN_LABEL_WEIGHT = {INTRO: 1.0, BUILD: 0.8, INSTRUMENTAL: 0.7}
_OTHER_LABEL_WEIGHT = 0.3


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

    def summary(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "start_bar": self.start_bar,
            "max_bars": self.max_bars,
            "label": self.label,
            "energy": None if self.energy is None else round(self.energy, 2),
            "vocal_free_bars": self.vocal_free_bars,
            "peak_bar": self.peak_bar,
            "score": round(self.score, 3),
            "reason": self.reason,
        }


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
    )


def _in_window(profile: TrackProfile, start_bar: int, label: str, why: str) -> MixWindow:
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
    score = (
        0.4 * label_weight + 0.15 * vocal_term + 0.15 * position + 0.15 * length_term + 0.15 * landing
    )
    reasons = [why, f"{max_bars} usable bars"]
    if peak is not None:
        reasons.append(f"lands on a peak at bar {peak}")
    if first_vocal is not None and first_vocal >= start_bar:
        reasons.append(f"first vocal at bar {first_vocal}")
    return MixWindow(
        role="in",
        start_bar=start_bar,
        max_bars=max_bars,
        label=label,
        energy=profile.mean_between(profile.bar_energy, start_bar, max_bars),
        vocal_free_bars=vocal_free,
        peak_bar=peak,
        score=score,
        reason="; ".join(reasons),
    )


def _offer_out_windows(
    profile: TrackProfile, candidates: dict[int, MixWindow], offer: Callable[[MixWindow], None]
) -> None:
    tail_from = max(1, int(profile.bars * 0.5))
    for section in profile.sections:
        if section.label in MIX_OUT_LABELS:
            _offer_if_open(offer, _out_window(profile, section.start_bar, section.label, f"{section.label} starts here"))
        elif section.start_bar >= tail_from:
            _offer_if_open(offer, _out_window(profile, section.start_bar, section.label, "phrase start in the back half"))
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


def mix_windows(profile: TrackProfile, role: Role, *, limit: int = 5) -> list[MixWindow]:
    """Ranked windows for ``role``, best first, at most ``limit``."""
    if role not in ROLES:
        raise ValueError(f"role must be 'out' or 'in', got {role!r}")
    candidates: dict[int, MixWindow] = {}

    def offer(window: MixWindow) -> None:
        current = candidates.get(window.start_bar)
        if current is None or window.score > current.score:
            candidates[window.start_bar] = window

    if role == "out":
        _offer_out_windows(profile, candidates, offer)
    else:
        _offer_in_windows(profile, candidates, offer)
    ranked = sorted(candidates.values(), key=lambda window: (-window.score, window.start_bar))
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
