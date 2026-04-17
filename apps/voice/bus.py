"""Event bus adapter.

Voice intents publish JSON events to the shared-state event bus
(``apps/shared/state/events``, Phase 5). Phase 5 has only partially
shipped at the time Phase 14 starts, so we always fall back to a
JSONL stub under ``data/voice/events.jsonl`` when the real bus does
not import cleanly.

This is the D6 integration boundary from CONTEXT. Follow the stub
policy: import is lazy, failure is logged once, tests mock.

TODO(phase-5): wire ``StateBackedBus`` through ``apps.shared.state.events``
when that module lands. Today Phase 5 only ships ``schema`` / ``db`` /
``ids`` / ``paths``. See phase-5 CONTEXT.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class EventBus(Protocol):
    """Protocol every bus backend implements."""

    def publish(self, event: dict) -> int:
        """Append an event and return its id (monotonic per backend)."""

    def recent(self, kind: str, limit: int = 10) -> list[dict]:
        """Return the most recent events of ``kind`` (best-effort)."""


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@dataclass
class JsonlStubBus:
    """Fallback event bus: append JSON lines to ``data/voice/events.jsonl``."""

    path: Path = field(
        default_factory=lambda: Path(__file__).resolve().parents[2]
        / "data"
        / "voice"
        / "events.jsonl"
    )
    _next_id: int = 1
    _cache: list[dict] = field(default_factory=list)

    def publish(self, event: dict) -> int:
        enriched = dict(event)
        enriched.setdefault("ts", _now_iso())
        enriched.setdefault("source", "voice")
        enriched["id"] = self._next_id
        self._next_id += 1
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(enriched, sort_keys=True))
            fh.write("\n")
        # Keep a bounded in-memory tail for ``recent`` lookups.
        self._cache.append(enriched)
        if len(self._cache) > 256:
            self._cache = self._cache[-256:]
        return enriched["id"]

    def recent(self, kind: str, limit: int = 10) -> list[dict]:
        matches = [e for e in self._cache if e.get("kind") == kind]
        return matches[-limit:]


@dataclass
class InMemoryBus:
    """Testing bus: publishes to a plain list, never touches disk."""

    events: list[dict] = field(default_factory=list)
    _next_id: int = 1

    def publish(self, event: dict) -> int:
        enriched = dict(event)
        enriched.setdefault("ts", _now_iso())
        enriched.setdefault("source", "voice")
        enriched["id"] = self._next_id
        self._next_id += 1
        self.events.append(enriched)
        return enriched["id"]

    def recent(self, kind: str, limit: int = 10) -> list[dict]:
        matches = [e for e in self.events if e.get("kind") == kind]
        return matches[-limit:]


class StateBackedBus:
    """Adapter for the Phase 5 state layer.

    Imports ``apps.shared.state.events.publish`` at construction time.
    If that module does not exist yet, the factory falls back to the
    stub bus and logs a once-per-run warning.

    TODO(phase-5): when Phase 5 ships ``events`` + ``writer``, swap this
    to a real wrapper that also reads the ``events`` table for ``recent``.
    """

    def __init__(self) -> None:
        # Imported here so the absence of the module is a RuntimeError we
        # can catch in ``make_bus`` -- NOT a module-load-time failure.
        from apps.shared.state import events as _events  # type: ignore[attr-defined]

        if not hasattr(_events, "publish"):
            raise RuntimeError(
                "apps.shared.state.events lacks publish(); Phase 5 "
                "events layer not yet shipped. Falling back to JSONL stub."
            )
        self._publish = _events.publish
        self._recent = getattr(_events, "recent", None)

    def publish(self, event: dict) -> int:
        enriched = dict(event)
        enriched.setdefault("ts", _now_iso())
        enriched.setdefault("source", "voice")
        return int(self._publish(enriched))

    def recent(self, kind: str, limit: int = 10) -> list[dict]:
        if self._recent is None:
            return []
        return list(self._recent(kind=kind, limit=limit))


_WARNED_STUB = False


def make_bus(force_stub: bool = False, warn=print) -> EventBus:
    """Factory: StateBackedBus when possible, else JsonlStubBus.

    ``force_stub`` is used by ``--dry-bus`` + tests. Emits a one-shot
    warning the first time the stub fallback activates.
    """
    global _WARNED_STUB
    if force_stub:
        return JsonlStubBus()
    try:
        return StateBackedBus()
    except (RuntimeError, ImportError, AttributeError) as exc:
        if not _WARNED_STUB:
            warn(
                "[voice] Phase 5 state layer missing or incomplete "
                f"({exc}); writing to JSONL stub bus at data/voice/events.jsonl"
            )
            _WARNED_STUB = True
        return JsonlStubBus()


def reset_warning() -> None:
    """Test helper: reset the once-per-run warning latch."""
    global _WARNED_STUB
    _WARNED_STUB = False
