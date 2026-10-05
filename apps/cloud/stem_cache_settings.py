"""Stem cache settings: the overridable knobs of the disk-aware budget.

Split out of :mod:`apps.cloud.stem_cache_budget`, which re-exports every name
here, so callers keep importing from the budget module. The policy the
defaults encode (why 20 GiB or 5%) is argued in that module's docstring.

Requirements (mini-PRD):
  ✔︎ ✅ 🎯 STEM-43 the knobs are overridable and never silently defaulted.
    [if] no settings file exists [then] the documented defaults load
    [if] a setting is malformed or unknown [then ⛔️] loading raises
    [if] one switch is saved [then] only that switch is stored, so a later
      default change still reaches this machine

-Claude
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_FLOOR_GIB: float = 20.0
DEFAULT_FLOOR_FRACTION: float = 0.05
DEFAULT_ENFORCE_INTERVAL_S: float = 300.0
SETTINGS_FILENAME: str = "stem-cache-settings.json"


class StemCacheSettingsError(ValueError):
    """Raised when the stem cache settings are malformed."""


@dataclass(frozen=True)
class StemCacheSettings:
    """The overridable knobs. Defaults are the documented policy.

    ``max_cache_gib`` is an optional hard cap for a user who wants the cache
    small regardless of how much disk is free. It only ever LOWERS the budget.
    """

    floor_gib: float = DEFAULT_FLOOR_GIB
    floor_fraction: float = DEFAULT_FLOOR_FRACTION
    max_cache_gib: float | None = None
    enforce_interval_s: float = DEFAULT_ENFORCE_INTERVAL_S
    auto_evict: bool = True


def settings_path(data_dir: Path) -> Path:
    return Path(data_dir) / "state" / SETTINGS_FILENAME


def _number(payload: Mapping[str, object], key: str, rule: str) -> float:
    """``payload[key]`` as a float. A bool is not a number here: ``true``
    would otherwise pass as 1.0."""
    value = payload[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StemCacheSettingsError(f"{key} must be a number {rule}; got {value!r}")
    return float(value)


def settings_from_mapping(payload: Mapping[str, object]) -> StemCacheSettings:
    """Validate a settings mapping. Unknown keys and bad values raise."""
    known = set(StemCacheSettings.__dataclass_fields__)
    unknown = set(payload).difference(known)
    if unknown:
        raise StemCacheSettingsError(
            f"unsupported stem cache setting(s): {sorted(unknown)}; known: {sorted(known)}"
        )
    merged: dict[str, object] = {**asdict(StemCacheSettings()), **payload}
    floor_gib = _number(merged, "floor_gib", ">= 0")
    fraction = _number(merged, "floor_fraction", "in [0, 1)")
    interval = _number(merged, "enforce_interval_s", "> 0")
    cap = (
        None
        if merged["max_cache_gib"] is None
        else _number(merged, "max_cache_gib", ">= 0, or null")
    )
    auto_evict = merged["auto_evict"]
    if floor_gib < 0:
        raise StemCacheSettingsError(f"floor_gib must be >= 0; got {floor_gib!r}")
    if not 0 <= fraction < 1:
        raise StemCacheSettingsError(f"floor_fraction must be in [0, 1); got {fraction!r}")
    if cap is not None and cap < 0:
        raise StemCacheSettingsError(f"max_cache_gib must be null or >= 0; got {cap!r}")
    if interval <= 0:
        raise StemCacheSettingsError(f"enforce_interval_s must be > 0; got {interval!r}")
    if not isinstance(auto_evict, bool):
        raise StemCacheSettingsError(f"auto_evict must be true or false; got {auto_evict!r}")
    return StemCacheSettings(
        floor_gib=floor_gib,
        floor_fraction=fraction,
        max_cache_gib=cap,
        enforce_interval_s=interval,
        auto_evict=auto_evict,
    )


def merged_settings(
    current: StemCacheSettings,
    overrides: Mapping[str, object],
    *,
    clear_max_cache_gib: bool = False,
) -> StemCacheSettings:
    """``current`` with ``overrides`` applied and validated. One merge shared
    by the HTTP route and the CLI verb, so both accept exactly the same
    partial update. ``clear_max_cache_gib`` removes the optional cap."""
    values: dict[str, object] = {**asdict(current), **overrides}
    if clear_max_cache_gib:
        values["max_cache_gib"] = None
    return settings_from_mapping(values)


def load_settings(data_dir: Path) -> StemCacheSettings:
    """Read the settings file. Absent is the documented defaults; present but
    malformed raises, so a typo can never silently restore a default floor."""
    path = settings_path(data_dir)
    if not path.is_file():
        return StemCacheSettings()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise StemCacheSettingsError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise StemCacheSettingsError(f"{path} must hold a JSON object")
    try:
        return settings_from_mapping(payload)
    except StemCacheSettingsError as exc:
        raise StemCacheSettingsError(f"{path}: {exc}") from exc


def save_settings(data_dir: Path, settings: StemCacheSettings) -> Path:
    """Persist only the fields that differ from the defaults. Writing every
    field pinned the defaults of the day into the file, so a later default
    change never reached a machine that had once toggled one switch."""
    validated = settings_from_mapping(asdict(settings))
    defaults = asdict(StemCacheSettings())
    overrides = {k: v for k, v in asdict(validated).items() if v != defaults[k]}
    path = settings_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomically(path, overrides)
    return path


def write_json_atomically(path: Path, payload: object) -> None:
    """Write ``payload`` to ``path`` through a temp file unique to this call.

    The timer enforcer and a post-hydrate enforce can write the same file at
    once; a shared ``<name>.tmp`` let one writer's replace consume the other's
    temp file, so the second replace raised FileNotFoundError and a good
    hydrate reported failure. Same directory, so the replace stays atomic.
    """
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise


__all__ = [
    "DEFAULT_ENFORCE_INTERVAL_S",
    "DEFAULT_FLOOR_FRACTION",
    "DEFAULT_FLOOR_GIB",
    "SETTINGS_FILENAME",
    "StemCacheSettings",
    "StemCacheSettingsError",
    "load_settings",
    "merged_settings",
    "save_settings",
    "settings_from_mapping",
    "settings_path",
    "write_json_atomically",
]
