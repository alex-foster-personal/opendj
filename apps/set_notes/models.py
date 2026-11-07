"""Set-note value types (SETNOTE-6340c)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Geometry:
    x: int | None = None
    y: int | None = None
    w: int | None = None
    h: int | None = None


@dataclass(slots=True)
class SetNote:
    scope: str
    body_md: str
    geometry: Geometry
    visible: bool
    rev: int
    updated_at: str


__all__ = ["Geometry", "SetNote"]
