"""LIGHT-10574: show planner turns sections, downbeats, bar energy and drop moments into a per-track show plan.

``plan_show(profile, sections, show_map, seed, moments=...)`` is pure and seeded. Rules (spec 07, L61):

- A look change lands on the downbeat nearest each section boundary (an off-grid boundary snaps).
- Effect intensity follows ``bar_energy`` in three bands (low / mid / high); when the profile has no
  ``bar_energy`` the band is the map cell's designed ``effect_level``.
- Each drop moment (fake drops included: the hit is still felt) gets a one-beat ``hit`` on the beat
  nearest to it, then the bar's band is restored.
- The same look never plays in two consecutive sections when the map cell offers another.
- The seed (with the track's stable id) makes every choice reproducible.

``moments`` is the ``/anlz`` ``phrase_moments`` list, passed in rather than read from
``apps.analysis_structure.drop_events`` because that module is not on its package's public contract.
``show_map`` is any object shaped like :class:`ShowMapLike` (the #9900 ``ShowMap`` satisfies it).
Band thresholds and fade lengths are placeholders until the intensity calibration (#9907) and the
show metrics (L63, #9903). Running the plan live is L62.
"""

from __future__ import annotations

import bisect
import random
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any, Final, NamedTuple, Protocol, TypeAlias

from apps.autodj.contract import TrackProfile
from apps.lighting.beat_clock import BEATS_PER_BAR
from apps.lighting.sections import LightSection, SectionSpan, TrackMood

LOW_ENERGY_MAX: Final = 0.55
MID_ENERGY_MAX: Final = 0.80
SECTION_FADE_BEATS: Final = 2
BAND_FADE_BEATS: Final = 4
HIT_BEATS: Final = 1


class EffectBand(StrEnum):
    LOW = "low"
    MID = "mid"
    HIGH = "high"
    HIT = "hit"


class MapCell(Protocol):
    @property
    def looks(self) -> tuple[str, ...]: ...

    @property
    def effect_level(self) -> str: ...


class ShowMapLike(Protocol):
    @property
    def autoloop(self) -> tuple[str, ...]: ...

    @property
    def autoloop_bars(self) -> int: ...

    def entry(self, section: LightSection, mood: TrackMood) -> MapCell: ...


class ShowCue(NamedTuple):
    start_beat: int
    look_id: str
    effects: EffectBand
    fade_beats: int


ShowPlan: TypeAlias = tuple[ShowCue, ...]


class ShowPlanError(ValueError):
    pass


class _Segment(NamedTuple):
    start_beat: int
    look_id: str
    designed_band: EffectBand


# ----------------------------------------------------------------------------- helpers


def _nearest_index(points: Sequence[float], t: float) -> int:
    i = bisect.bisect_left(points, t)
    if i <= 0:
        return 0
    if i >= len(points):
        return len(points) - 1
    return i - 1 if t - points[i - 1] <= points[i] - t else i


def _band_of(energy: float) -> EffectBand:
    if energy < LOW_ENERGY_MAX:
        return EffectBand.LOW
    if energy < MID_ENERGY_MAX:
        return EffectBand.MID
    return EffectBand.HIGH


def _bar_band(profile: TrackProfile, bar: int, designed: EffectBand) -> EffectBand:
    if profile.bar_energy is None:
        return designed
    return _band_of(profile.bar_energy[bar])


def _segments(
    profile: TrackProfile, sections: Sequence[SectionSpan], show_map: ShowMapLike, rng: random.Random
) -> list[_Segment]:
    if all(span.section is LightSection.UNKNOWN for span in sections):
        loop, step = show_map.autoloop, show_map.autoloop_bars
        offset = rng.randrange(len(loop))
        return [
            _Segment(bar * BEATS_PER_BAR, loop[(offset + n) % len(loop)], EffectBand.MID)
            for n, bar in enumerate(range(0, len(profile.downbeats_s), step))
        ]
    starts: dict[int, SectionSpan] = {}
    for n, span in enumerate(sections):
        bar = 0 if n == 0 else _nearest_index(profile.downbeats_s, span.start_ms / 1000.0)
        starts[bar * BEATS_PER_BAR] = span
    segments: list[_Segment] = []
    for beat in sorted(starts):
        span = starts[beat]
        cell = show_map.entry(span.section, span.mood)
        previous = segments[-1].look_id if segments else None
        candidates = [look for look in cell.looks if look != previous] or list(cell.looks)
        segments.append(_Segment(beat, rng.choice(candidates), EffectBand(cell.effect_level)))
    return segments


def _segment_at(segments: Sequence[_Segment], beat: int) -> _Segment:
    return segments[bisect.bisect_right([s.start_beat for s in segments], beat) - 1]


def _base_cues(profile: TrackProfile, segments: Sequence[_Segment]) -> dict[int, ShowCue]:
    cues: dict[int, ShowCue] = {}
    previous_band: EffectBand | None = None
    starts = {s.start_beat for s in segments}
    for bar in range(len(profile.downbeats_s)):
        beat = bar * BEATS_PER_BAR
        segment = _segment_at(segments, beat)
        band = _bar_band(profile, bar, segment.designed_band)
        if beat in starts:
            cues[beat] = ShowCue(beat, segment.look_id, band, SECTION_FADE_BEATS)
        elif band != previous_band:
            cues[beat] = ShowCue(beat, segment.look_id, band, BAND_FADE_BEATS)
        previous_band = band
    return cues


def _apply_hits(
    profile: TrackProfile, segments: Sequence[_Segment], cues: dict[int, ShowCue], moments: Sequence[Mapping[str, Any]]
) -> None:
    beats = beat_times_s(profile)
    for moment in moments:
        if moment["kind"] != "drop":
            continue
        hit = _nearest_index(beats, float(moment["time_s"]))
        existing = cues.get(hit)
        if existing is not None:
            cues[hit] = existing._replace(effects=EffectBand.HIT)
        else:
            cues[hit] = ShowCue(hit, _segment_at(segments, hit).look_id, EffectBand.HIT, 0)
        after = hit + HIT_BEATS
        if after < len(beats) and after not in cues:
            segment = _segment_at(segments, after)
            band = _bar_band(profile, after // BEATS_PER_BAR, segment.designed_band)
            cues[after] = ShowCue(after, segment.look_id, band, 0)


# ----------------------------------------------------------------------------- public api


def beat_times_s(profile: TrackProfile) -> tuple[float, ...]:
    """Every beat's time; each bar between downbeats split into ``BEATS_PER_BAR`` equal beats."""
    if not profile.downbeats_s:
        raise ShowPlanError(f"{profile.stable_id}: no downbeats to plan a show on")
    ends = (*profile.downbeats_s[1:], profile.downbeats_s[-1] + profile.bar_s)
    return tuple(
        start + (end - start) * k / BEATS_PER_BAR
        for start, end in zip(profile.downbeats_s, ends, strict=True)
        for k in range(BEATS_PER_BAR)
    )


def plan_show(
    profile: TrackProfile,
    sections: Sequence[SectionSpan],
    show_map: ShowMapLike,
    seed: int,
    *,
    moments: Sequence[Mapping[str, Any]],
) -> ShowPlan:
    """Beat-indexed cues for one track, in beat order, at most one per beat."""
    if not profile.downbeats_s:
        raise ShowPlanError(f"{profile.stable_id}: no downbeats to plan a show on")
    if not sections:
        raise ShowPlanError(f"{profile.stable_id}: no sections")
    if profile.bar_energy is not None and len(profile.bar_energy) != len(profile.downbeats_s):
        raise ShowPlanError(
            f"{profile.stable_id}: {len(profile.bar_energy)} bar_energy values for {len(profile.downbeats_s)} downbeats"
        )
    rng = random.Random(f"{seed}:{profile.stable_id}")
    segments = _segments(profile, sections, show_map, rng)
    cues = _base_cues(profile, segments)
    _apply_hits(profile, segments, cues, moments)
    return tuple(cues[beat] for beat in sorted(cues))
