"""Dataclasses + type aliases for the state layer.

``ProvenanceValue`` is the open-dj v0 strawman envelope (section 4.1):
``{value, source, confidence?, modified_at}``. ``Event`` is the in-process
event-bus message, which is also what lands in the durable ``events``
table.
"""
from __future__ import annotations

import dataclasses
import typing

Source = typing.Literal[
    "mik",
    "rekordbox",
    "djay",
    "serato",
    "traktor",
    "open-dj-tool",
    "manual",
    "inferred",
    "webui",
]

# Mirror of the CHECK constraint in schema.py. Kept in sync by test.
SOURCES: frozenset[str] = frozenset(typing.get_args(Source))


@dataclasses.dataclass(frozen=True)
class ProvenanceValue:
    """A provenance-wrapped value -- open-dj v0 strawman section 4.1."""

    value: typing.Any
    source: Source
    modified_at: str  # RFC 3339 UTC.
    confidence: float | None = None

    def as_open_dj(self) -> dict[str, typing.Any]:
        """Return the canonical open-dj envelope (sorted keys; no None)."""
        payload: dict[str, typing.Any] = {
            "modified_at": self.modified_at,
            "source": self.source,
            "value": self.value,
        }
        if self.confidence is not None:
            payload["confidence"] = self.confidence
        return payload


@dataclasses.dataclass(frozen=True)
class Event:
    """In-process event record. ``payload`` is JSON-encodable."""

    ts: str
    kind: str
    stable_id: str | None
    payload: dict[str, typing.Any]
    actor: str | None = None


__all__ = ["Source", "SOURCES", "ProvenanceValue", "Event"]
