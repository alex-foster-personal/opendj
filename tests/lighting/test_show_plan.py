"""LIGHT-10574: show planner turns sections, downbeats, bar energy and drop moments into a show plan.

[if] 6 sections give fewer than 6 look changes or one lands off a downbeat [then] fail, [else stop].
[if] two consecutive same-type sections share a look when the map offers two [then] fail, [else stop].
[if] the same profile and seed give two different plans [then] fail, [else stop].
[if] a drop hit misses the beat nearest the drop moment [then] fail, [else stop].
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from apps.autodj.contract import TrackProfile
from apps.lighting.beat_clock import BEATS_PER_BAR
from apps.lighting.sections import LightSection, TrackMood, resolve_sections
from apps.lighting.show_plan import EffectBand, ShowCue, ShowPlanError, plan_show

pytestmark = [pytest.mark.requirement("LIGHT-10574")]

FIXTURES = Path(__file__).parent / "fixtures"
SECTION_STARTS = [0, 32, 64, 96, 128, 160]


@dataclasses.dataclass(frozen=True)
class _TestCell:
    looks: tuple[str, ...]
    effect_level: str = "mid"


@dataclasses.dataclass(frozen=True)
class _TestMap:
    autoloop: tuple[str, ...] = ("loop_a", "loop_b", "loop_c")
    autoloop_bars: int = 8

    def entry(self, section: LightSection, mood: TrackMood) -> _TestCell:
        return _TestCell((f"{section}_a", f"{section}_b"))


@dataclasses.dataclass(frozen=True)
class _OneLookMap(_TestMap):
    def entry(self, section: LightSection, mood: TrackMood) -> _TestCell:
        return _TestCell(("only",))


def _data() -> dict[str, Any]:
    return json.loads((FIXTURES / "track_profile_sample.json").read_text())


def _profile(data: dict[str, Any]) -> TrackProfile:
    p = data["profile"]
    return TrackProfile(
        stable_id=p["stable_id"],
        title=None,
        artist=None,
        bpm=p["bpm"],
        key=None,
        duration_s=p["duration_s"],
        downbeats_s=tuple(p["downbeats_s"]),
        grid_trusted=True,
        grid_note=None,
        sections=(),
        sections_source="own",
        bar_energy=tuple(p["bar_energy"]),
        bar_bass=None,
        bar_vocals=None,
        vocals_status="not_analyzed",
    )


def _plan(seed: int = 7, show_map: _TestMap | None = None, **overrides: Any) -> tuple[ShowCue, ...]:
    data = _data()
    profile = dataclasses.replace(_profile(data), **overrides)
    sections = resolve_sections(data["anlz"])
    return plan_show(profile, sections, show_map or _TestMap(), seed, moments=data["anlz"]["phrase_moments"])


def _look_at(plan: tuple[ShowCue, ...], beat: int) -> str:
    return [cue for cue in plan if cue.start_beat <= beat][-1].look_id


def test_six_sections_give_six_look_changes_on_downbeats() -> None:
    """[if] 6 sections give fewer than 6 look changes, or one is off a downbeat [then] fail."""
    plan = _plan()
    changes = [
        cue for prev, cue in zip((None, *plan), plan, strict=False) if prev is None or cue.look_id != prev.look_id
    ]
    assert len(changes) >= 6
    assert all(cue.start_beat % BEATS_PER_BAR == 0 for cue in changes)
    assert [cue.start_beat for cue in changes] == SECTION_STARTS


def test_consecutive_same_type_sections_get_different_looks() -> None:
    """[if] the two consecutive drops share a look with a two-look map [then] fail."""
    for seed in range(20):
        plan = _plan(seed)
        assert _look_at(plan, 64) != _look_at(plan, 96)


def test_same_profile_and_seed_plan_equal() -> None:
    """[if] the same profile and seed plan differently, or the seed is ignored [then] fail."""
    assert _plan(7) == _plan(7)
    assert any(_plan(seed) != _plan(7) for seed in range(20))


def test_drop_hit_lands_on_nearest_beat() -> None:
    """[if] a drop hit is not on the beat nearest the drop moment [then] fail."""
    by_beat = {cue.start_beat: cue for cue in _plan()}
    assert by_beat[64].effects is EffectBand.HIT
    assert by_beat[65].effects is EffectBand.HIGH
    data = _data()
    plan = plan_show(
        _profile(data), resolve_sections(data["anlz"]), _TestMap(), 7, moments=[{"kind": "drop", "time_s": 41.4}]
    )
    hits = [cue.start_beat for cue in plan if cue.effects is EffectBand.HIT]
    assert hits == [83]


def test_effects_follow_bar_energy_bands() -> None:
    """[if] a drop bar is not high, an intro bar not low, or no-energy cues ignore the map level [then] fail."""
    by_beat = {cue.start_beat: cue for cue in _plan()}
    assert by_beat[0].effects is EffectBand.LOW
    assert by_beat[96].effects is EffectBand.HIGH
    flat = _plan(bar_energy=None)
    assert {cue.effects for cue in flat if cue.start_beat in SECTION_STARTS and cue.start_beat != 64} == {
        EffectBand.MID
    }


def test_no_phrases_uses_autoloop() -> None:
    """[if] a track with no phrases does not cycle the autoloop every 8 bars [then] fail."""
    data = _data()
    sections = resolve_sections({"phrases": []}, duration_s=96.0)
    plan = plan_show(_profile(data), sections, _TestMap(), 7, moments=[])
    changes = [
        cue for prev, cue in zip((None, *plan), plan, strict=False) if prev is None or cue.look_id != prev.look_id
    ]
    assert [cue.start_beat for cue in changes] == SECTION_STARTS
    loop = list(_TestMap().autoloop)
    first = loop.index(changes[0].look_id)
    assert [cue.look_id for cue in changes] == [loop[(first + n) % 3] for n in range(6)]


def test_single_look_map_repeats_rather_than_failing() -> None:
    """[if] a one-look map raises or changes look [then] fail."""
    plan = _plan(show_map=_OneLookMap())
    assert {cue.look_id for cue in plan} == {"only"}


def test_empty_downbeats_raises() -> None:
    """[if] a profile with no downbeats is planned silently [then] fail."""
    with pytest.raises(ShowPlanError, match="sample-show-plan"):
        _plan(downbeats_s=(), bar_energy=None)
