"""Section-to-look map as data (LIGHT-10561b, spec item LIGHT-60, issue #9900).

``show_map.json`` holds rekordbox-style per-(section, mood) ranked looks plus effect
intensity, and a SoundSwitch-style autoloop for tracks without phrases. Calibration edits
the JSON only; picking among ranked looks is L61 / #9901.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from apps.lighting.sections import LightSection, SectionSpan, TrackMood

MIN_LOOKS: Final = 2
DEFAULT_MAP_PATH: Final = Path(__file__).with_name("show_map.json")
DEFAULT_LOOKS_PATH: Final = Path(__file__).with_name("looks_default.json")
DEFAULT_AUTOLOOP_BARS: Final = 8

_MAP_TOP_KEYS: Final = frozenset(
    {"version", "note", "looks_file", "autoloop_bars", "autoloop", "sections"}
)
_CELL_KEYS: Final = frozenset({"looks", "effect_level"})
_SECTION_NAMES: Final = frozenset(s.value for s in LightSection)
_MOOD_NAMES: Final = frozenset(m.value for m in TrackMood)


class EffectLevel(StrEnum):
    LOW = "low"
    MID = "mid"
    HIGH = "high"


_EFFECT_LEVELS: Final = frozenset(e.value for e in EffectLevel)


class ShowMapError(ValueError):
    """Raised when map or looks data fails validation."""

    def __init__(self, source: str, errors: Sequence[str]) -> None:
        self.errors = tuple(errors)
        super().__init__(f"invalid show map {source}:\n" + "\n".join(f"  {e}" for e in self.errors))


@dataclass(frozen=True)
class MapEntry:
    looks: tuple[str, ...]
    effect_level: EffectLevel


@dataclass(frozen=True)
class ShowMap:
    entries: Mapping[tuple[LightSection, TrackMood], MapEntry]
    autoloop: tuple[str, ...]
    autoloop_bars: int
    look_ids: frozenset[str]

    def entry(self, section: LightSection, mood: TrackMood) -> MapEntry:
        return self.entries[(section, mood)]

    def uses_autoloop(self, spans: Sequence[SectionSpan]) -> bool:
        if not spans:
            return True
        return all(span.section is LightSection.UNKNOWN for span in spans)

    def autoloop_look(self, bar_index: int) -> str:
        if bar_index < 0:
            raise ValueError(f"bar_index must be non-negative, got {bar_index}")
        slot = (bar_index // self.autoloop_bars) % len(self.autoloop)
        return self.autoloop[slot]

    def autoloop_changes(self, downbeats_ms: Sequence[float]) -> list[tuple[float, str]]:
        step = self.autoloop_bars
        return [
            (downbeats_ms[index], self.autoloop_look(index))
            for index in range(0, len(downbeats_ms), step)
        ]


# ----- validation helpers --------------------------------------------------


class _Errors:
    def __init__(self) -> None:
        self.items: list[str] = []

    def add(self, path: str, reason: str) -> None:
        self.items.append(f"{path}: {reason}")


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ShowMapError(str(path), [f"$: file not found: {exc}"]) from exc
    except json.JSONDecodeError as exc:
        raise ShowMapError(str(path), [f"$: not valid JSON: {exc}"]) from exc


def _object_keys(
    value: object, path: str, allowed: frozenset[str], errors: _Errors
) -> Mapping[str, Any] | None:
    if not isinstance(value, dict):
        errors.add(path, f"must be an object, got {type(value).__name__}")
        return None
    for key in sorted(set(value) - allowed):
        errors.add(f"{path}.{key}", f"unknown key; allowed: {sorted(allowed)}")
    return value


def _parse_look_ids(data: object, source: str) -> frozenset[str]:
    errors = _Errors()
    obj = _object_keys(data, "$", frozenset({"version", "note", "looks"}), errors)
    if obj is None:
        raise ShowMapError(source, errors.items)
    version = obj.get("version")
    if version != 1:
        errors.add("$.version", f"must be 1, got {version!r}")
    raw_looks = obj.get("looks")
    if not isinstance(raw_looks, list):
        errors.add("$.looks", "must be a list")
    ids: list[str] = []
    seen: set[str] = set()
    if isinstance(raw_looks, list):
        for index, item in enumerate(raw_looks):
            item_path = f"$.looks[{index}]"
            if not isinstance(item, dict):
                errors.add(item_path, f"must be an object, got {type(item).__name__}")
                continue
            look_id = item.get("id")
            if not isinstance(look_id, str) or not look_id.strip():
                errors.add(f"{item_path}.id", "must be a non-empty string")
                continue
            if look_id in seen:
                errors.add(f"{item_path}.id", f"duplicate look id {look_id!r}")
            seen.add(look_id)
            ids.append(look_id)
    if errors.items:
        raise ShowMapError(source, errors.items)
    return frozenset(ids)


def _validate_look_ref(path: str, look_id: object, known: frozenset[str], errors: _Errors) -> str | None:
    if not isinstance(look_id, str) or not look_id:
        errors.add(path, "must be a non-empty string")
        return None
    if look_id not in known:
        errors.add(path, f"unknown look id {look_id!r}")
        return None
    return look_id


def _parse_cell(
    raw: object,
    path: str,
    known: frozenset[str],
    errors: _Errors,
) -> MapEntry | None:
    obj = _object_keys(raw, path, _CELL_KEYS, errors)
    if obj is None:
        return None
    raw_looks = obj.get("looks")
    if not isinstance(raw_looks, list):
        errors.add(f"{path}.looks", "must be a list")
        return None
    parsed_ids: list[str] = []
    for index, item in enumerate(raw_looks):
        pid = _validate_look_ref(f"{path}.looks[{index}]", item, known, errors)
        if pid is not None:
            parsed_ids.append(pid)
    if len(parsed_ids) < MIN_LOOKS:
        errors.add(f"{path}.looks", f"at least {MIN_LOOKS} looks required, got {len(parsed_ids)}")
    if len(parsed_ids) != len(set(parsed_ids)):
        errors.add(f"{path}.looks", "duplicate look ids in cell")
    level_raw = obj.get("effect_level")
    if level_raw not in _EFFECT_LEVELS:
        errors.add(f"{path}.effect_level", f"must be one of {sorted(_EFFECT_LEVELS)}, got {level_raw!r}")
        return None
    if len(parsed_ids) < MIN_LOOKS:
        return None
    return MapEntry(tuple(parsed_ids), EffectLevel(str(level_raw)))


def _parse_sections_block(
    raw: object,
    known: frozenset[str],
    errors: _Errors,
) -> dict[tuple[LightSection, TrackMood], MapEntry]:
    entries: dict[tuple[LightSection, TrackMood], MapEntry] = {}
    if not isinstance(raw, dict):
        errors.add("sections", f"must be an object, got {type(raw).__name__}")
        return entries
    for key in sorted(set(raw) - _SECTION_NAMES):
        errors.add(f"sections.{key}", f"unknown section; allowed: {sorted(_SECTION_NAMES)}")
    for section in LightSection:
        sec_path = f"sections.{section.value}"
        sec_raw = raw.get(section.value)
        if sec_raw is None:
            errors.add(sec_path, "missing")
            continue
        if not isinstance(sec_raw, dict):
            errors.add(sec_path, f"must be an object, got {type(sec_raw).__name__}")
            continue
        for mood_key in sorted(set(sec_raw) - _MOOD_NAMES):
            errors.add(f"{sec_path}.{mood_key}", f"unknown mood; allowed: {sorted(_MOOD_NAMES)}")
        for mood in TrackMood:
            mood_path = f"{sec_path}.{mood.value}"
            mood_raw = sec_raw.get(mood.value)
            if mood_raw is None:
                errors.add(mood_path, "missing")
                continue
            entry = _parse_cell(mood_raw, mood_path, known, errors)
            if entry is not None:
                entries[(section, mood)] = entry
    return entries


def _parse_autoloop(raw: object, known: frozenset[str], errors: _Errors) -> tuple[str, ...]:
    if not isinstance(raw, list):
        errors.add("autoloop", "must be a list")
        return ()
    parsed: list[str] = []
    for index, item in enumerate(raw):
        pid = _validate_look_ref(f"autoloop[{index}]", item, known, errors)
        if pid is not None:
            parsed.append(pid)
    if len(parsed) < MIN_LOOKS:
        errors.add("autoloop", f"at least {MIN_LOOKS} looks required, got {len(parsed)}")
    if len(parsed) != len(set(parsed)):
        errors.add("autoloop", "duplicate look ids in autoloop")
    return tuple(parsed)


def _parse_autoloop_bars(raw: object, errors: _Errors) -> int:
    if raw is None:
        return DEFAULT_AUTOLOOP_BARS
    if not isinstance(raw, int) or isinstance(raw, bool):
        errors.add("$.autoloop_bars", f"must be an integer >= 1, got {raw!r}")
        return DEFAULT_AUTOLOOP_BARS
    if raw < 1:
        errors.add("$.autoloop_bars", f"must be >= 1, got {raw}")
        return DEFAULT_AUTOLOOP_BARS
    return raw


def parse_show_map(
    data: object,
    source: str,
    look_ids: frozenset[str],
) -> ShowMap:
    errors = _Errors()
    obj = _object_keys(data, "$", _MAP_TOP_KEYS, errors)
    if obj is None:
        raise ShowMapError(source, errors.items)
    if obj.get("version") != 1:
        errors.add("$.version", f"must be 1, got {obj.get('version')!r}")
    autoloop_bars = _parse_autoloop_bars(obj.get("autoloop_bars"), errors)
    autoloop = _parse_autoloop(obj.get("autoloop"), look_ids, errors)
    entries = _parse_sections_block(obj.get("sections"), look_ids, errors)
    if errors.items:
        raise ShowMapError(source, errors.items)
    expected = len(LightSection) * len(TrackMood)
    if len(entries) != expected:
        raise ShowMapError(
            source,
            [f"$.sections: expected {expected} cells, got {len(entries)}"],
        )
    return ShowMap(entries, autoloop, autoloop_bars, look_ids)


# ----- entry points --------------------------------------------------------


def load_look_ids(path: Path = DEFAULT_LOOKS_PATH) -> frozenset[str]:
    """Read and validate the looks catalog; returns the set of look ids."""
    return _parse_look_ids(_read_json(path), str(path))


def load_show_map(path: Path = DEFAULT_MAP_PATH, looks_path: Path | None = None) -> ShowMap:
    """Read and validate the section-to-look map and its looks file."""
    map_path = Path(path)
    data = _read_json(map_path)
    if not isinstance(data, dict):
        raise ShowMapError(str(map_path), ["$: must be an object"])
    looks_file = data.get("looks_file")
    if looks_path is None:
        if not isinstance(looks_file, str) or not looks_file.strip():
            raise ShowMapError(str(map_path), ["$.looks_file: must be a non-empty string"])
        looks_path = map_path.parent / looks_file
    look_ids = load_look_ids(looks_path)
    return parse_show_map(data, str(map_path), look_ids)
