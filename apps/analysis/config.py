"""Tiny loader for ``apps/analysis/config.yaml``.  Cached by path."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .lane_enums import DEFAULT_FIRST_IMPORT_LANES, LANES
from .queue_user_lanes import FIRST_IMPORT_FORBIDDEN_LANES

CONFIG_PATH: Path = Path(__file__).resolve().parent / "config.yaml"


class FirstImportLaneError(ValueError):
    """A lane cannot be named or enqueued on first import."""


@lru_cache(maxsize=8)
def load_config(path: str | None = None) -> dict[str, Any]:
    p = Path(path) if path else CONFIG_PATH
    with open(p, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def first_import_lanes(*, config: dict[str, Any] | None = None) -> tuple[str, ...]:
    """The yaml-backed fast lane list for first folder import.

    Reads ``first_import.lanes`` via :func:`load_config` when ``config`` is
    omitted. Every entry must be a shipped analysis record lane and must not
    name a user job or deferred detector lane.
    """
    raw = load_config() if config is None else config
    section = raw.get("first_import")
    if not isinstance(section, dict):
        raise TypeError(f"apps/analysis/config.yaml must define first_import.lanes; got first_import={section!r}")
    lanes = section.get("lanes")
    if not isinstance(lanes, list) or not lanes:
        raise TypeError(f"first_import.lanes must be a non-empty list of lane names; got {lanes!r}")
    out: list[str] = []
    for lane in lanes:
        if not isinstance(lane, str) or not lane:
            raise TypeError(f"first_import.lanes entry must be a non-empty str; got {lane!r}")
        if lane in FIRST_IMPORT_FORBIDDEN_LANES:
            raise FirstImportLaneError(
                f"first_import.lanes must not name slow or deferred lane {lane!r}; "
                f"forbidden are {FIRST_IMPORT_FORBIDDEN_LANES}"
            )
        if lane not in LANES:
            raise FirstImportLaneError(f"unknown first_import lane {lane!r}; LANES are {LANES}")
        if lane not in DEFAULT_FIRST_IMPORT_LANES:
            raise FirstImportLaneError(
                f"lane {lane!r} is not a default fast first-import lane; defaults are {DEFAULT_FIRST_IMPORT_LANES}"
            )
        if lane in out:
            raise FirstImportLaneError(f"first_import.lanes lists {lane!r} more than once")
        out.append(lane)
    return tuple(out)


def assert_first_import_lane_allowed(lane: str, *, config: dict[str, Any] | None = None) -> str:
    """Fail loud when a caller asks whether a forbidden lane runs on first import."""
    if lane in FIRST_IMPORT_FORBIDDEN_LANES:
        raise FirstImportLaneError(
            f"lane {lane!r} is not a first-import lane; forbidden are {FIRST_IMPORT_FORBIDDEN_LANES}"
        )
    allowed = first_import_lanes(config=config)
    if lane not in allowed:
        raise FirstImportLaneError(f"lane {lane!r} is not listed for first import; configured lanes are {allowed}")
    return lane


__all__ = [
    "CONFIG_PATH",
    "FirstImportLaneError",
    "assert_first_import_lane_allowed",
    "first_import_lanes",
    "load_config",
]
