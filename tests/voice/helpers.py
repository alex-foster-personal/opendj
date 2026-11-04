"""Shared helpers for apps/voice unit tests."""
from __future__ import annotations

from typing import Any

from apps.voice import grammar


def make_intent(
    kind: str,
    slots: dict[str, Any] | None = None,
    raw: str = "",
) -> grammar.Intent:
    return grammar.Intent(kind=kind, slots=slots or {}, raw_transcript=raw)


def events_of_kind(bus: Any, kind: str) -> list[dict[str, Any]]:
    return [event for event in bus.events if event.get("kind") == kind]
