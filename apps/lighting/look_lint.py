"""Cross-reference linter for the looks catalog, show map and ambient JSON (issue #14516, spec 07 section 3).

Reuses the loaders in ``show_map`` and ``ambient`` for schema and look-id checks, then checks the
optional per-look fields that those loaders skip: group targets and effect groups against a patch,
effect names against ``effects.EFFECTS``, and ``fade_beats`` as a positive whole number of beats.
Pure: reads JSON files only, no clock, no sockets.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from apps.lighting.ambient import DEFAULT_AMBIENT_PATH, AmbientError, load_ambient_config
from apps.lighting.effects import EFFECTS
from apps.lighting.show_map import DEFAULT_LOOKS_PATH, DEFAULT_MAP_PATH, ShowMapError, load_look_ids, load_show_map

__all__ = ["LintIssue", "lint_files", "lint_looks"]

_LOOK_KEYS: Final = frozenset({"id", "name", "groups", "effects", "fade_beats"})


@dataclass(frozen=True)
class LintIssue:
    source: str
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.source}:{self.path}: {self.message}"


def _split(source: str, errors: Sequence[str]) -> list[LintIssue]:
    out: list[LintIssue] = []
    for line in errors:
        path, _, message = line.partition(": ")
        out.append(LintIssue(source, path, message))
    return out


def _check_fade(raw: object, path: str, source: str) -> list[LintIssue]:
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return [LintIssue(source, path, f"must be a positive whole number of beats, got {raw!r}")]
    if raw <= 0 or raw != int(raw):
        return [LintIssue(source, path, f"must be a positive whole number of beats, got {raw!r}")]
    return []


def _check_groups(
    names: Iterable[object], path: str, source: str, patch_groups: frozenset[str] | None
) -> list[LintIssue]:
    out: list[LintIssue] = []
    for name in names:
        if not isinstance(name, str) or not name:
            out.append(LintIssue(source, path, f"group name must be a non-empty string, got {name!r}"))
        elif patch_groups is not None and name not in patch_groups:
            out.append(LintIssue(source, path, f"group {name!r} is not in the patch"))
    return out


def _check_effects(raw: object, path: str, source: str, patch_groups: frozenset[str] | None) -> list[LintIssue]:
    if not isinstance(raw, list):
        return [LintIssue(source, path, "must be a list")]
    out: list[LintIssue] = []
    for index, item in enumerate(raw):
        item_path = f"{path}[{index}]"
        if not isinstance(item, dict):
            out.append(LintIssue(source, item_path, "must be an object"))
            continue
        effect_id = item.get("effect_id")
        if effect_id not in EFFECTS:
            out.append(
                LintIssue(source, f"{item_path}.effect_id", f"unknown effect {effect_id!r}; known: {sorted(EFFECTS)}")
            )
        groups = item.get("groups", [])
        if not isinstance(groups, list):
            out.append(LintIssue(source, f"{item_path}.groups", "must be a list"))
        else:
            out += _check_groups(groups, f"{item_path}.groups", source, patch_groups)
    return out


def lint_looks(data: object, source: str, patch_groups: frozenset[str] | None = None) -> list[LintIssue]:
    """Lint optional per-look fields (groups, effects, fade_beats); ``patch_groups`` None skips patch checks."""
    looks = data.get("looks") if isinstance(data, dict) else None
    if not isinstance(looks, list):
        return []
    out: list[LintIssue] = []
    for index, look in enumerate(looks):
        if not isinstance(look, dict):
            continue
        path = f"$.looks[{index}]"
        out += [LintIssue(source, f"{path}.{key}", "unknown key") for key in sorted(set(look) - _LOOK_KEYS)]
        if "groups" in look:
            groups = look["groups"]
            if isinstance(groups, dict):
                out += _check_groups(groups, f"{path}.groups", source, patch_groups)
            else:
                out.append(LintIssue(source, f"{path}.groups", "must be an object"))
        if "effects" in look:
            out += _check_effects(look["effects"], f"{path}.effects", source, patch_groups)
        if "fade_beats" in look:
            out += _check_fade(look["fade_beats"], f"{path}.fade_beats", source)
    return out


def lint_files(
    looks_path: Path = DEFAULT_LOOKS_PATH,
    map_path: Path = DEFAULT_MAP_PATH,
    ambient_path: Path = DEFAULT_AMBIENT_PATH,
    patch_groups: frozenset[str] | None = None,
) -> list[LintIssue]:
    """Lint the looks catalog, show map and ambient file together; empty list means clean."""
    out: list[LintIssue] = []
    try:
        load_look_ids(looks_path)
    except ShowMapError as exc:
        return _split(str(looks_path), exc.errors)
    out += lint_looks(json.loads(Path(looks_path).read_text(encoding="utf-8")), str(looks_path), patch_groups)
    try:
        load_show_map(map_path, looks_path)
    except ShowMapError as exc:
        out += _split(str(map_path), exc.errors)
    try:
        load_ambient_config(ambient_path)
    except AmbientError as exc:
        out += _split(str(ambient_path), exc.errors)
    except ShowMapError as exc:
        out += _split(str(ambient_path), exc.errors)
    return sorted(set(out), key=lambda i: (i.source, i.path, i.message))
