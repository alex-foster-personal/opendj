"""Public lighting types, clock selection policy and service protocols.

Code outside the package imports this module; delivery layers may also import
``apps.lighting.http``. This contract depends only on the standard library.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

CONTRACT_VERSION = "lighting/1"

#: Feature flag that gates every lighting route and output (default off).
LIGHTING_FLAG_ID = "lighting.output"
CLOCK_SOURCES = frozenset({"master", "deck-1", "deck-2", "deck-3", "deck-4"})


class ClockSourceError(ValueError):
    """A refused clock-source update, independent of its delivery layer."""

    status_code: int
    code: str

    def __init__(self, status_code: int, code: str) -> None:
        self.status_code = status_code
        self.code = code
        super().__init__(code)


def parse_clock_source(source: str) -> str:
    """Accept only exact canonical selectors, without a guessed default."""
    if not isinstance(source, str) or source not in CLOCK_SOURCES:
        raise ClockSourceError(400, "lighting_clock_bad_deck")
    return source


def normalize_clock_deck(value: bool | int | float | str | None) -> str | None:
    """Normalize the mirror's numeric deck identifiers without accepting booleans."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 1 <= value <= 4:
        return str(value)
    if isinstance(value, str) and value in {"1", "2", "3", "4"}:
        return value
    return None


@dataclass(frozen=True)
class ClockState:
    clock_source: str
    effective_deck: str | None
    unavailable_reason: str | None


@dataclass(frozen=True)
class ClockSnapshot:
    clock_source: str = "master"
    master_deck: str | None = None
    loaded_decks: frozenset[str] = frozenset()
    unavailable_reason: str | None = None

    def resolve(self) -> ClockState:
        """Resolve one coherent mirror snapshot, retaining the requested preference."""
        source = parse_clock_source(self.clock_source)
        if self.unavailable_reason is not None:
            return ClockState(source, None, self.unavailable_reason)
        deck = self.master_deck if source == "master" else source.removeprefix("deck-")
        if deck is None:
            return ClockState(source, None, "lighting_clock_master_unavailable")
        if deck not in self.loaded_decks:
            return ClockState(source, None, "lighting_clock_deck_empty")
        return ClockState(source, deck, None)


class ClockController(Protocol):
    def snapshot(self) -> ClockSnapshot: ...

    def read(self) -> ClockState: ...

    def set_source(self, source: str) -> ClockState: ...


__all__ = [
    "CLOCK_SOURCES",
    "CONTRACT_VERSION",
    "LIGHTING_FLAG_ID",
    "ClockController",
    "ClockSnapshot",
    "ClockSourceError",
    "ClockState",
    "normalize_clock_deck",
    "parse_clock_source",
]
