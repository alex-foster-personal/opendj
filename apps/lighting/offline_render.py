"""LIGHT-73 offline render: a track's planned show composed to frames on a virtual clock (#9904).

``render_show`` plans the show (:func:`apps.lighting.show_plan.plan_show`), shapes it under the
``--response`` energy curve, and plays it through the live show engine: one
:class:`apps.lighting.runtime.LightingRuntime` fed the track as a single playing deck on a virtual
clock, one ``process`` tick per frame from ``from_s`` to ``to_s`` at ``fps``. The runtime picks the
beat, cue, colour owner and layer intensity exactly as it does live; this module only turns each
show frame into a frames-websocket message (the palette colours of the colour deck's look, one
per fixture in patch order, scaled by the cue's energy-curve intensity, strobing on the cue's first
beat when the curve strobes it). Output is the replay file shape ``{"patch": ..., "frames": [...]}``
that ``opendj lighting replay`` and the stage visualiser read. Same inputs and seed give the same bytes.
"""

from __future__ import annotations

import bisect
import itertools
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from apps.autodj.contract import AutoDjService, TrackProfile
from apps.lighting.beat_clock import BEATS_PER_BAR, DeckSample, GridBeat
from apps.lighting.patch import VenueStore
from apps.lighting.runtime import DeckFeed, FeedFrame, LightingRuntime
from apps.lighting.sections import SectionSpan, resolve_sections
from apps.lighting.show_map import Palette, ShowMap
from apps.lighting.show_plan import EffectBand, ShapedCue, ShowPlan, beat_times_s, plan_show, shape_plan
from apps.lighting.show_runtime import DeckPlan, ShowCue, ShowFrame, current_cue
from apps.lighting.stage_feed import parse_frame, stage_patch_body

NS_PER_S = 1_000_000_000
DECK = "1"
EFFECT_LEVEL: dict[EffectBand, int] = {EffectBand.LOW: 0, EffectBand.MID: 1, EffectBand.HIGH: 2, EffectBand.HIT: 2}


class OfflineRenderError(ValueError):
    pass


def _compact_profile(raw: Mapping[str, Any]) -> TrackProfile:
    return TrackProfile(
        stable_id=str(raw["stable_id"]),
        title=None,
        artist=None,
        bpm=float(raw["bpm"]),
        key=None,
        duration_s=float(raw["duration_s"]),
        downbeats_s=tuple(float(t) for t in raw["downbeats_s"]),
        grid_trusted=True,
        grid_note=None,
        sections=(),
        sections_source="own",
        bar_energy=tuple(float(e) for e in raw["bar_energy"]) if raw.get("bar_energy") is not None else None,
        bar_bass=None,
        bar_vocals=None,
        vocals_status="not_analyzed",
    )


def load_analysis(
    analysis_dir: Path, track_id: str, *, autodj: AutoDjService
) -> tuple[TrackProfile, Mapping[str, Any]]:
    """Read ``<dir>/<track_id>.json`` (``/anlz`` payload, or ``{profile, anlz}``); fail naming a missing one."""
    path = Path(analysis_dir) / f"{track_id}.json"
    if not path.is_file():
        raise OfflineRenderError(f"{track_id}: missing analysis (no {path})")
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "profile" in data and "anlz" in data:
        return _compact_profile(data["profile"]), data["anlz"]
    if not isinstance(data, dict):
        raise OfflineRenderError(f"{track_id}: analysis {path} is not a JSON object")
    return autodj.profile_from_payloads(data), data


def deck_plan(track_id: str, plan: ShowPlan, sections: Sequence[SectionSpan], beats_s: Sequence[float]) -> DeckPlan:
    """The planner's cues as the live runtime's deck plan; each section start is a phrase boundary."""
    cues = tuple(
        ShowCue(cue.start_beat, cue.look_id, EFFECT_LEVEL[cue.effects], hit=cue.effects is EffectBand.HIT)
        for cue in plan
    )
    phrases = sorted({_nearest_beat(beats_s, span.start_ms / 1000.0) for span in sections})
    return DeckPlan(track_id=track_id, cues=cues, phrase_beats=tuple(phrases))


def _nearest_beat(beats_s: Sequence[float], t: float) -> int:
    i = bisect.bisect_left(beats_s, t)
    if i <= 0:
        return 0
    if i >= len(beats_s):
        return len(beats_s) - 1
    return i - 1 if t - beats_s[i - 1] <= beats_s[i] - t else i


def _grid(beats_s: Sequence[float]) -> list[GridBeat]:
    """The beat grid the runtime reads: downbeat on every fourth beat, each beat's BPM from its own spacing."""
    gaps = [b - a for a, b in itertools.pairwise(beats_s)]
    bpms = [60.0 / gap if gap > 0 else 0.0 for gap in gaps]
    bpms.append(bpms[-1] if bpms else 0.0)
    return [
        {"time_ms": t * 1000.0, "n": i % BEATS_PER_BAR + 1, "bpm": bpm}
        for i, (t, bpm) in enumerate(zip(beats_s, bpms, strict=True))
    ]


def _fixture_frame(
    show: ShowFrame,
    beat_index: int,
    plan: DeckPlan,
    shaped: Mapping[int, ShapedCue],
    palette: Palette,
    fixtures: Sequence[str],
) -> list[dict[str, Any]]:
    colour = next((layer for layer in show.layers if layer.deck == show.colour_deck), None)
    if show.ambient or colour is None:
        return [_fixture(fid, (0, 0, 0), 0.0, 0.0) for fid in fixtures]
    cue = current_cue(plan, beat_index)
    shape = shaped[cue.at_beat]
    level = min(1.0, sum(layer.intensity for layer in show.layers))
    strobe = shape.strobe_hz if beat_index == cue.at_beat else 0.0
    colours = palette.colours.get(colour.look) or ((255, 255, 255),)
    return [
        _fixture(fid, colours[n % len(colours)], round(shape.intensity * level, 6), strobe)
        for n, fid in enumerate(fixtures)
    ]


def _fixture(fid: str, rgb: Sequence[int], intensity: float, strobe_hz: float) -> dict[str, Any]:
    return {
        "id": fid,
        "rgb": list(rgb),
        "intensity": intensity,
        "pan_deg": None,
        "tilt_deg": None,
        "strobe_hz": strobe_hz,
    }


def render_show(  # noqa: PLR0913
    profile: TrackProfile,
    anlz: Mapping[str, Any],
    store: VenueStore,
    show_map: ShowMap,
    palette: Palette,
    *,
    from_s: float,
    to_s: float,
    fps: int,
    seed: int,
    response: str = "matched",
) -> dict[str, Any]:
    """Frames file ``{patch, frames}`` for ``[from_s, to_s)`` at ``fps``, played by the live runtime on a virtual clock."""
    if fps <= 0 or to_s <= from_s:
        raise OfflineRenderError(f"need fps > 0 and to_s > from_s (got fps {fps}, {from_s}..{to_s})")
    sections = resolve_sections(anlz, duration_s=profile.duration_s)
    plan = plan_show(profile, sections, show_map, seed, moments=anlz.get("phrase_moments") or [])
    shaped = {cue.start_beat: cue for cue in shape_plan(profile, sections, plan, response)}
    beats = beat_times_s(profile)
    live_plan = deck_plan(profile.stable_id, plan, sections, beats)
    grid = _grid(beats)
    runtime = LightingRuntime()
    runtime.palette = palette
    patch = stage_patch_body(store)
    fixtures = [fx["id"] for fx in patch["fixtures"]]
    from_ns = round(from_s * NS_PER_S)
    count = round((to_s - from_s) * fps)
    frames: list[dict[str, Any]] = []
    for i in range(count):
        t_ns = from_ns + i * NS_PER_S // fps
        sample = DeckSample(deck=DECK, position_ms=t_ns / 1_000_000, rate=1.0, playing=True, host_time_ns=t_ns)
        feed = FeedFrame(host_time_ns=t_ns, decks=(DeckFeed(sample, grid, sections, live_plan),), weights={DECK: 1.0})
        show = runtime.process(feed)
        (deck,) = runtime.show_decks(feed)
        frames.append(
            parse_frame(
                {
                    "t_ns": t_ns,
                    "patch_version": patch["version"],
                    "fixtures": _fixture_frame(show, deck.beat_index, live_plan, shaped, palette, fixtures),
                }
            )
        )
    return {"patch": patch, "frames": frames}


def dump_frames(rendered: Mapping[str, Any]) -> str:
    """Stable JSON text for a rendered frames file."""
    return json.dumps(rendered, sort_keys=True, separators=(",", ":")) + "\n"
