"""AutoPlay's early decision (AUTOPLAN-02, AUTOPLAN-03): where to leave the master, and which track to bring in.

AutoPlay used to decide 16 s before the end. It now decides around minute three, so it has to look ahead: every phrase
start past the earliest bar it can still reach is ranked by structure (outro or breakdown over chorus), vocals, falling
energy and the drop's position, and by how far past the soft target it sits. The weights are mimicked hygiene from the
canon doc and Mixxx's outro cue, round 0 in (private path), not fitted to anything.

The track comes from the bundle's chooser (ADR-0130) run over the proposals of a shortlist AutoPlay's own rules built: the
intent's style order, then variety, then M as a floor. Nothing is rendered here, so the proposal's rule score stands in
for M and the answer says it is unmeasured; plan_live applies the real floor once the pair has rendered.

No window and no plan are answers, not errors: ``Decision.track`` is None and ``reason`` says why, so AutoPlay can fall
back and say why.

Requirements:
  ✔︎ only phrase starts past the earliest bar are offered
  ✔︎ a soft target is preferred over a better window far past it, and a much better window near it beats a plain one
  ✔︎ the chooser picks the track: intent, then variety, with the score a floor
Acceptance:
  [if] the track has no phrase analysis [then ⛔️] no windows, never an invented boundary
  [if] a chorus starts within the guard [then] the phrase start before it ranks below a clear one
  [if] an energy_up intent has a cut into one track and a higher-scored blend into another [then] the cut's track is chosen
  [if] no shortlisted track has a valid plan [then ⛔️] no track, and the reason names what was tried
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from apps.autodj.choose import Candidate, choose_for_set
from apps.autodj.live import with_in_track
from apps.autodj.plan import parse_plan
from apps.autodj.profile import TrackProfile
from apps.autodj.propose import propose_transition
from apps.autodj.sections import PEAK_LABELS
from apps.autodj.windows import _OTHER_LABEL_WEIGHT, _OUT_LABEL_WEIGHT, MixWindow, window_at

#: A chorus or drop at a window start, or this close after it, would be blended over.
PEAK_GUARD_BARS = 8
#: Bars before a window whose energy it is compared with.
ENERGY_LOOKBACK_BARS = 8
#: Past this many bars beyond the target a window takes the whole distance penalty.
DISTANCE_SPAN_BARS = 64
_LABEL_WEIGHT, _VOCAL_WEIGHT, _ENERGY_WEIGHT, _SPENT_WEIGHT = 0.5, 0.2, 0.15, 0.15
_PEAK_PENALTY, _DISTANCE_PENALTY = 0.3, 0.4
#: How many ranked windows each shortlisted track may try before it is reported as having no plan.
WINDOWS_TRIED = 3
#: Windows returned for the readout and the CLI.
WINDOWS_SHOWN = 5


@dataclass(frozen=True)
class Decision:
    """The window and track AutoPlay will plan for, or neither and why."""

    reason: str
    track: str | None = None
    window: MixWindow | None = None
    style: str | None = None
    bars: int | None = None
    windows: tuple[MixWindow, ...] = ()
    #: Shortlisted tracks with no plan, and why: {"track": ..., "reason": ...}.
    rejected: tuple[Mapping[str, str], ...] = field(default_factory=tuple)


def _peak_within(profile: TrackProfile, bar: int) -> bool:
    return any(
        section.label in PEAK_LABELS and bar <= section.start_bar <= bar + PEAK_GUARD_BARS
        for section in profile.sections
    )


def _drop_spent(profile: TrackProfile, bar: int) -> bool:
    """A peak section ends where this window starts, so nothing is lost by leaving."""
    return any(section.label in PEAK_LABELS and section.end_bar == bar for section in profile.sections)


def _energy_fall(profile: TrackProfile, window: MixWindow) -> float:
    start = max(1, window.start_bar - ENERGY_LOOKBACK_BARS)
    before = profile.mean_between(profile.bar_energy, start, window.start_bar - start)
    if before is None or window.energy is None:
        return 0.0
    return max(0.0, min(1.0, before - window.energy))


def _ahead_score(profile: TrackProfile, window: MixWindow, target_bar: float) -> float:
    label = _OUT_LABEL_WEIGHT.get(window.label, _OTHER_LABEL_WEIGHT)
    vocal = 1.0 if window.vocal_free_bars is None else window.vocal_free_bars / max(1, window.max_bars)
    distance = min(1.0, max(0.0, window.start_bar - target_bar) / DISTANCE_SPAN_BARS)
    return (
        _LABEL_WEIGHT * label
        + _VOCAL_WEIGHT * vocal
        + _ENERGY_WEIGHT * _energy_fall(profile, window)
        + (_SPENT_WEIGHT if _drop_spent(profile, window.start_bar) else 0.0)
        - (_PEAK_PENALTY if _peak_within(profile, window.start_bar) else 0.0)
        - _DISTANCE_PENALTY * distance
    )


def windows_ahead(profile: TrackProfile, *, earliest_bar: float, target_bar: float) -> list[MixWindow]:
    """Every phrase start past ``earliest_bar`` that still leaves a bar to blend in, best first."""
    ranked: list[MixWindow] = []
    for section in profile.sections:
        if section.start_bar <= earliest_bar or section.start_bar >= profile.bars:
            continue
        window = window_at(profile, "out", section.start_bar)
        ranked.append(replace(window, score=_ahead_score(profile, window, target_bar), reason=f"{section.label} phrase; {window.reason}"))
    return sorted(ranked, key=lambda window: (-window.score, window.start_bar))


def _plans_into(
    out_profile: TrackProfile,
    in_profile: TrackProfile,
    windows: Sequence[MixWindow],
    decks: tuple[int, int],
    intent: str | None,
    live: Mapping[str, Any],
    min_out_bar: float,
) -> tuple[list[dict[str, Any]], MixWindow | None]:
    """The proposals leaving from the best window that has any, and that window."""
    out_deck, in_deck = decks
    planned = with_in_track(live, in_deck, in_profile.stable_id, in_profile.bpm)
    for window in windows[:WINDOWS_TRIED]:
        rows = propose_transition(
            out_profile,
            in_profile,
            out_deck=out_deck,
            in_deck=in_deck,
            intent=intent,
            min_out_bar=min_out_bar,
            live=planned,
            top=None,
            around_bar=float(window.start_bar),
        )["proposals"]
        here = [row for row in rows if parse_plan(row["plan"]).out.start_bar == window.start_bar]
        if here:
            return here, window
    return [], None


def decide(
    out_profile: TrackProfile,
    shortlist: Sequence[TrackProfile],
    *,
    decks: tuple[int, int],
    intent: str | None,
    recent_styles: Sequence[str],
    earliest_bar: float,
    target_bar: float,
    live: Mapping[str, Any],
) -> Decision:
    """The window and the shortlisted track to plan for, from everything the shortlist can do from the master's windows."""
    windows = windows_ahead(out_profile, earliest_bar=earliest_bar, target_bar=target_bar)
    if not windows:
        return Decision(reason=f"no phrase start left in the master past bar {earliest_bar:.0f}")
    shown = tuple(windows[:WINDOWS_SHOWN])
    pool: list[Candidate] = []
    chosen: dict[str, tuple[TrackProfile, MixWindow, dict[str, Any]]] = {}
    rejected: list[Mapping[str, str]] = []
    for in_profile in shortlist:
        rows, window = _plans_into(out_profile, in_profile, windows, decks, intent, live, earliest_bar)
        if window is None:
            rejected.append({"track": in_profile.stable_id, "reason": "no valid plan from the master's best windows"})
            continue
        for index, row in enumerate(rows):
            cid = f"{in_profile.stable_id}#{index}"
            chosen[cid] = (in_profile, window, row)
            pool.append(Candidate(cid, str(row["style"]), float(row["score"]), False))
    pick = choose_for_set(pool, intent, recent_styles)
    if pick is None:
        return Decision(reason=f"no shortlisted track has a valid plan ({len(shortlist)} tried)", windows=shown, rejected=tuple(rejected))
    in_profile, window, row = chosen[pick.cid]
    reason = f"{row['style']} over {row['bars']} bars at bar {window.start_bar} ({window.label}); M not measured until it renders"
    return Decision(
        reason=reason,
        track=in_profile.stable_id,
        window=window,
        style=str(row["style"]),
        bars=int(row["bars"]),
        windows=shown,
        rejected=tuple(rejected),
    )
