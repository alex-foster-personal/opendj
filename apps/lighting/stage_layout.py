"""Stage layout for the visualiser: where each patched fixture sits in 3D and where it points.

The layout file is JSON, one entry per patched fixture id:

    {"version": 1, "fixtures": [
      {"id": "par-left-1", "x": 0.1, "y": 0.2, "z": 0.5, "rotation": 90.0, "beam_angle": 25.0}]}

x runs left to right from the audience, y front to back, z floor to truss, all 0..1 (as in
``apps.lighting.patch``). ``rotation`` is degrees 0..360 about the vertical axis, ``beam_angle`` the
full beam spread in degrees, more than 0 and at most 180. Every id must be in the patch; two
fixtures at the same x/y/z load but produce a warning.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from apps.lighting.patch import Patch, PatchError

LAYOUT_FILE_VERSION = 1

_STORE_KEYS = frozenset({"version", "fixtures"})
_FIXTURE_KEYS = frozenset({"id", "x", "y", "z", "rotation", "beam_angle"})


@dataclass(frozen=True)
class FixturePlacement:
    id: str
    x: float
    y: float
    z: float
    rotation: float
    beam_angle: float


@dataclass(frozen=True)
class StageLayout:
    fixtures: tuple[FixturePlacement, ...]


@dataclass(frozen=True)
class LayoutResult:
    layout: StageLayout
    warnings: tuple[str, ...]


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _placement(raw: object, path: str, patch: Patch, errors: list[str]) -> FixturePlacement | None:
    if not isinstance(raw, dict):
        errors.append(f"{path}: must be an object, got {type(raw).__name__}")
        return None
    bad = len(errors)
    errors.extend(
        f"{path}.{key}: unknown key; allowed: {sorted(_FIXTURE_KEYS)}" for key in sorted(set(raw) - _FIXTURE_KEYS)
    )
    errors.extend(f"{path}.{key}: missing" for key in sorted(_FIXTURE_KEYS - set(raw)))
    fixture_id = raw.get("id")
    if "id" in raw and not isinstance(fixture_id, str):
        errors.append(f"{path}.id: must be a string, got {fixture_id!r}")
    elif isinstance(fixture_id, str) and fixture_id not in {f.id for f in patch.fixtures}:
        errors.append(f"{path}.id: fixture {fixture_id!r} is not in patch {patch.name!r}")
    limits = {"x": (0.0, 1.0), "y": (0.0, 1.0), "z": (0.0, 1.0), "rotation": (0.0, 360.0), "beam_angle": (0.0, 180.0)}
    for key, (low, high) in limits.items():
        if key not in raw:
            continue
        value = raw[key]
        if not _number(value) or not low <= value <= high or (key == "beam_angle" and value == 0):
            errors.append(f"{path}.{key}: must be a number in {low:g}..{high:g}, got {value!r}")
    if len(errors) > bad:
        return None
    return FixturePlacement(
        raw["id"], float(raw["x"]), float(raw["y"]), float(raw["z"]), float(raw["rotation"]), float(raw["beam_angle"])
    )


def layout_from_json(data: object, patch: Patch, source: str = "<data>") -> LayoutResult:
    """Parse a layout against ``patch``; raise ``PatchError`` listing every problem."""
    if not isinstance(data, dict):
        raise PatchError(source, [f"$: must be an object, got {type(data).__name__}"])
    errors = [f"$.{key}: unknown key; allowed: {sorted(_STORE_KEYS)}" for key in sorted(set(data) - _STORE_KEYS)]
    if data.get("version") != LAYOUT_FILE_VERSION:
        errors.append(f"$.version: unsupported version {data.get('version')!r}; expected {LAYOUT_FILE_VERSION}")
    raw_fixtures = data.get("fixtures")
    if not isinstance(raw_fixtures, list):
        errors.append(f"$.fixtures: must be a list, got {raw_fixtures!r}")
        raw_fixtures = []
    placements: list[FixturePlacement] = []
    seen: dict[str, int] = {}
    for index, raw in enumerate(raw_fixtures):
        path = f"$.fixtures[{index}]"
        placement = _placement(raw, path, patch, errors)
        if placement is None:
            continue
        if placement.id in seen:
            errors.append(f"{path}.id: duplicate id {placement.id!r} (also $.fixtures[{seen[placement.id]}])")
            continue
        seen[placement.id] = index
        placements.append(placement)
    if errors:
        raise PatchError(source, errors)
    warnings = [
        f"fixtures {a.id!r} and {b.id!r} share position ({a.x:g}, {a.y:g}, {a.z:g})"
        for i, a in enumerate(placements)
        for b in placements[i + 1 :]
        if (a.x, a.y, a.z) == (b.x, b.y, b.z)
    ]
    return LayoutResult(StageLayout(tuple(placements)), tuple(warnings))


def layout_to_json(layout: StageLayout) -> dict[str, Any]:
    """Dump a layout to its JSON file shape."""
    return {
        "version": LAYOUT_FILE_VERSION,
        "fixtures": [
            {"id": f.id, "x": f.x, "y": f.y, "z": f.z, "rotation": f.rotation, "beam_angle": f.beam_angle}
            for f in layout.fixtures
        ],
    }
