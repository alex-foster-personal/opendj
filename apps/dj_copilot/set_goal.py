"""Structured goal grammar for PLAY IT (Phase 13, PLAY-02).

Phase 14 (voice) parses spoken English into this dataclass; Phase 13
only ships the dataclass + CLI surface.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

_MIN_DURATION_MIN: int = 30
_MAX_DURATION_MIN: int = 240
_MIN_ENERGY: int = 1
_MAX_ENERGY: int = 10


@dataclass(slots=True)
class SetGoal:
    """Six-knob description of a desired set shape."""

    duration_min: int
    peak_at_min: int | None = None
    floor_energy: int = 3
    ceiling_energy: int = 9
    open_on_key: str | None = None
    close_on_energy: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.duration_min, int):
            raise ValueError(
                f"duration_min must be int, got {self.duration_min!r}"
            )
        if not (_MIN_DURATION_MIN <= self.duration_min <= _MAX_DURATION_MIN):
            raise ValueError(
                f"duration_min {self.duration_min} outside "
                f"[{_MIN_DURATION_MIN}, {_MAX_DURATION_MIN}]"
            )
        if self.peak_at_min is not None:
            if not isinstance(self.peak_at_min, int):
                raise ValueError(
                    f"peak_at_min must be int or None, got {self.peak_at_min!r}"
                )
            if not (0 <= self.peak_at_min <= self.duration_min):
                raise ValueError(
                    f"peak_at_min {self.peak_at_min} outside "
                    f"[0, duration_min={self.duration_min}]"
                )
        for name, val in (
            ("floor_energy", self.floor_energy),
            ("ceiling_energy", self.ceiling_energy),
        ):
            if not isinstance(val, int):
                raise ValueError(f"{name} must be int, got {val!r}")
            if not (_MIN_ENERGY <= val <= _MAX_ENERGY):
                raise ValueError(
                    f"{name} {val} outside [{_MIN_ENERGY}, {_MAX_ENERGY}]"
                )
        if self.floor_energy > self.ceiling_energy:
            raise ValueError(
                f"floor_energy ({self.floor_energy}) must be <= "
                f"ceiling_energy ({self.ceiling_energy})"
            )
        if self.close_on_energy is not None:
            if not isinstance(self.close_on_energy, int):
                raise ValueError(
                    f"close_on_energy must be int or None, "
                    f"got {self.close_on_energy!r}"
                )
            if not (_MIN_ENERGY <= self.close_on_energy <= _MAX_ENERGY):
                raise ValueError(
                    f"close_on_energy {self.close_on_energy} outside "
                    f"[{_MIN_ENERGY}, {_MAX_ENERGY}]"
                )
        if self.open_on_key is not None and not isinstance(
            self.open_on_key, str
        ):
            raise ValueError(
                f"open_on_key must be str or None, got {self.open_on_key!r}"
            )


def set_goal_to_dict(g: SetGoal) -> dict[str, Any]:
    return asdict(g)


def set_goal_from_dict(d: dict[str, Any]) -> SetGoal:
    return SetGoal(
        duration_min=int(d["duration_min"]),
        peak_at_min=(
            None if d.get("peak_at_min") is None else int(d["peak_at_min"])
        ),
        floor_energy=int(d.get("floor_energy", 3)),
        ceiling_energy=int(d.get("ceiling_energy", 9)),
        open_on_key=d.get("open_on_key"),
        close_on_energy=(
            None
            if d.get("close_on_energy") is None
            else int(d["close_on_energy"])
        ),
    )


def set_goal_to_json(g: SetGoal) -> str:
    return json.dumps(set_goal_to_dict(g), sort_keys=True, separators=(",", ":"))


def set_goal_from_json(s: str) -> SetGoal:
    return set_goal_from_dict(json.loads(s))
