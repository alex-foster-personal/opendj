"""Look model: per-group static targets and linear crossfades between looks (LIGHT-10575, spec LIGHT-50 / L50).

Pure: no clock reads, no I/O. Fade length in beats is supplied by the caller via ``fade_progress``;
unset ``GroupTarget`` fields hold the previous look's value. Linear RGB and degree interpolation
mimics QLC+'s default fade behaviour (public docs).

Sources (public documents only):
- QLC+: default crossfade is linear in channel values (docs.qlcplus.org).
- (private path) (item L50).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

RGB = tuple[int, int, int]

__all__ = [
    "EffectRef",
    "FixtureState",
    "GroupTarget",
    "Look",
    "LookError",
    "build_look",
    "crossfade",
    "fade_progress",
    "resolve",
]


class LookError(ValueError):
    """A look names an unknown group, or a target holds an out-of-range value."""


def _require_finite(name: str, value: float) -> None:
    if not math.isfinite(value):
        raise LookError(f"{name} must be finite, got {value}")


def _require_unit(name: str, value: float) -> None:
    _require_finite(name, value)
    if not 0.0 <= value <= 1.0:
        raise LookError(f"{name} {value} is outside 0.0..1.0")


def _require_rgb(rgb: RGB) -> None:
    for i, ch in enumerate(rgb):
        if not isinstance(ch, int) or not 0 <= ch <= 255:
            raise LookError(f"rgb channel {i} {ch!r} is outside 0..255")


@dataclass(frozen=True)
class GroupTarget:
    rgb: RGB | None = None
    intensity: float | None = None
    white: float | None = None
    pan_deg: float | None = None
    tilt_deg: float | None = None

    def __post_init__(self) -> None:
        if self.rgb is not None:
            _require_rgb(self.rgb)
        if self.intensity is not None:
            _require_unit("intensity", self.intensity)
        if self.white is not None:
            _require_unit("white", self.white)
        if self.pan_deg is not None:
            _require_finite("pan_deg", self.pan_deg)
        if self.tilt_deg is not None:
            _require_finite("tilt_deg", self.tilt_deg)


@dataclass(frozen=True)
class EffectRef:
    effect_id: str
    groups: tuple[str, ...] = ()
    params: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.effect_id:
            raise LookError("effect_id must be non-empty")


@dataclass(frozen=True)
class FixtureState:
    rgb: RGB | None = None
    intensity: float | None = None
    white: float | None = None
    pan_deg: float | None = None
    tilt_deg: float | None = None


@dataclass(frozen=True)
class Look:
    id: str
    name: str
    groups: Mapping[str, GroupTarget]
    effects: tuple[EffectRef, ...] = ()
    fixtures: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id:
            raise LookError("look id must be non-empty")
        effects = self.effects
        if not isinstance(effects, tuple):
            object.__setattr__(self, "effects", tuple(effects))


def _group_map(patch: Mapping[str, Sequence[str]] | object) -> dict[str, tuple[str, ...]]:
    if isinstance(patch, Mapping):
        raw = patch
    else:
        groups = getattr(patch, "groups", None)
        if groups is None or not isinstance(groups, Mapping):
            raise LookError("patch has no groups mapping")
        raw = groups
    return {name: tuple(fixture_ids) for name, fixture_ids in raw.items()}


def build_look(
    id: str,
    name: str,
    groups: Mapping[str, GroupTarget],
    effects: Sequence[EffectRef] = (),
    *,
    patch: Mapping[str, Sequence[str]] | object,
) -> Look:
    gmap = _group_map(patch)
    missing: set[str] = set()
    for group_name in groups:
        if group_name not in gmap or len(gmap[group_name]) == 0:
            missing.add(group_name)
    for effect in effects:
        for group_name in effect.groups:
            if group_name not in gmap or len(gmap[group_name]) == 0:
                missing.add(group_name)
    if missing:
        names = ", ".join(f"'{g}'" for g in sorted(missing))
        raise LookError(f"look {id!r}: group {names} names no fixture in the patch")
    fixtures = {g: gmap[g] for g in groups}
    return Look(id, name, dict(groups), tuple(effects), fixtures=fixtures)


def _overlay_state(base: FixtureState, target: GroupTarget) -> FixtureState:
    rgb = target.rgb if target.rgb is not None else base.rgb
    intensity = target.intensity if target.intensity is not None else base.intensity
    white = target.white if target.white is not None else base.white
    pan_deg = target.pan_deg if target.pan_deg is not None else base.pan_deg
    tilt_deg = target.tilt_deg if target.tilt_deg is not None else base.tilt_deg
    return FixtureState(rgb=rgb, intensity=intensity, white=white, pan_deg=pan_deg, tilt_deg=tilt_deg)


def resolve(
    look: Look,
    previous: Mapping[str, FixtureState] | None = None,
) -> dict[str, FixtureState]:
    out: dict[str, FixtureState] = dict(previous or {})
    for group_name, target in look.groups.items():
        fixture_ids = look.fixtures.get(group_name)
        if fixture_ids is None:
            raise LookError(f"look {look.id!r}: group {group_name!r} has no fixtures mapping")
        for fixture_id in fixture_ids:
            base = out.get(fixture_id, FixtureState())
            out[fixture_id] = _overlay_state(base, target)
    return out


def _lerp_float(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _lerp_rgb(a: RGB, b: RGB, t: float) -> RGB:
    return tuple(int(_lerp_float(a[i], b[i], t) + 0.5) for i in range(3))  # type: ignore[misc]


def _blend_field(
    sa: float | None,
    sb: float | None,
    t: float,
) -> float | None:
    if sa is None and sb is None:
        return None
    if sa is None:
        return sb
    if sb is None:
        return sa
    return _lerp_float(sa, sb, t)


def _blend_rgb(sa: RGB | None, sb: RGB | None, t: float) -> RGB | None:
    if sa is None and sb is None:
        return None
    if sa is None:
        return sb
    if sb is None:
        return sa
    return _lerp_rgb(sa, sb, t)


def crossfade(
    a: Look,
    b: Look,
    progress: float,
    previous: Mapping[str, FixtureState] | None = None,
) -> dict[str, FixtureState]:
    if not math.isfinite(progress) or not 0.0 <= progress <= 1.0:
        raise ValueError(f"crossfade progress {progress!r} is outside 0.0..1.0")
    start = resolve(a, previous)
    end = resolve(b, start)
    all_ids = sorted(set(start) | set(end))
    out: dict[str, FixtureState] = {}
    for fixture_id in all_ids:
        s = start.get(fixture_id, FixtureState())
        e = end.get(fixture_id, FixtureState())
        out[fixture_id] = FixtureState(
            rgb=_blend_rgb(s.rgb, e.rgb, progress),
            intensity=_blend_field(s.intensity, e.intensity, progress),
            white=_blend_field(s.white, e.white, progress),
            pan_deg=_blend_field(s.pan_deg, e.pan_deg, progress),
            tilt_deg=_blend_field(s.tilt_deg, e.tilt_deg, progress),
        )
    return out


def fade_progress(elapsed_beats: float, fade_beats: float) -> float:
    if not math.isfinite(elapsed_beats) or not math.isfinite(fade_beats):
        raise ValueError(f"fade_progress expects finite beats, got {elapsed_beats!r}, {fade_beats!r}")
    if fade_beats < 0:
        raise ValueError(f"fade_beats {fade_beats!r} must not be negative")
    if fade_beats == 0:
        return 1.0
    return min(1.0, max(0.0, elapsed_beats / fade_beats))
