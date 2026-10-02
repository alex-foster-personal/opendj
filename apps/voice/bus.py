"""Event bus adapter.

Voice intents publish JSON events to the shared-state event bus
(:mod:`apps.shared.state.events`, Phase 5). ``StateBackedBus`` now
writes into Phase 5's durable ``events`` table via
:func:`apps.shared.state.db.open_rw` and fans events out on an
:class:`apps.shared.state.events.EventBus` instance.

This is the D6 integration boundary from CONTEXT. A ``JsonlStubBus``
fallback remains for the ``--dry-bus`` / test-without-state-db path,
but Phase 5 is shipped and the real bus is now the default.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from apps.voice.settings import voice_data_dir

log = logging.getLogger(__name__)


@runtime_checkable
class EventBus(Protocol):
    """Protocol every bus backend implements."""

    def publish(self, event: dict) -> int:
        """Append an event and return its id (monotonic per backend)."""

    def recent(self, kind: str, limit: int = 10) -> list[dict]:
        """Return the most recent events of ``kind`` (best-effort)."""


def _now_iso() -> str:
    return datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@dataclass
class JsonlStubBus:
    """Fallback event bus: append JSON lines to ``<data dir>/voice/events.jsonl``."""

    path: Path = field(
        default_factory=lambda: voice_data_dir() / "events.jsonl"
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

    Holds one :class:`apps.shared.state.events.EventBus` for in-process
    fanout and one :class:`sqlite3.Connection` (opened through
    :func:`apps.shared.state.db.open_rw`) for durable appends to the
    ``events`` table. ``recent`` reads back from the same table so
    voice consumers (``/voice/recent``) see every publish -- including
    events other phases wrote.

    ``publish`` continues to accept a dict (Phase 14 callers have not
    adopted :class:`apps.shared.state.types.Event` yet) and returns the
    monotonic row id SQLite hands back, matching the Phase 14
    :class:`EventBus` protocol.
    """

    def __init__(
        self,
        *,
        db_path: Path | None = None,
    ) -> None:
        # Imported lazily so earlier phases of the repo (or a test env
        # where apps.shared.state was deleted) surface ImportError to
        # make_bus, which catches and falls back to the JSONL stub.
        from apps.shared.state import db as _state_db
        from apps.shared.state.events import EventBus as _EventBus
        from apps.shared.state.types import Event as _Event

        self._Event = _Event
        self._conn = _state_db.open_rw(
            Path(db_path) if db_path is not None else None
        )
        self._bus = _EventBus()
        self._lock = threading.Lock()

    def publish(self, event: dict) -> int:
        enriched = dict(event)
        enriched.setdefault("ts", _now_iso())
        enriched.setdefault("source", "voice")
        kind = str(enriched.get("kind", ""))
        stable_id = enriched.get("stable_id")
        # payload is the full enriched dict so ``recent`` can reconstruct
        # the caller's original shape (slots, bpm, arbitrary extras).
        payload_json = json.dumps(enriched, sort_keys=True)
        safe_stable_id = stable_id if isinstance(stable_id, (str, type(None))) else None
        # Hold the lock across INSERT, commit, and in-process fanout so that
        # a durable events row and the in-process EventBus stay in sync. The
        # explicit commit (adversarial R4 F2) makes the row survive a daemon
        # crash; fanout failures are trapped (adversarial R4 F1) so a noisy
        # subscriber or a closed in-process bus cannot mask the durable write
        # or bring the producer down. A structured error log surfaces the
        # desync loudly rather than silently.
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO events (ts, kind, stable_id, payload_json, actor) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    enriched["ts"],
                    kind,
                    stable_id,
                    payload_json,
                    "voice",
                ),
            )
            new_id = int(cur.lastrowid or 0)
            self._conn.commit()
            enriched["id"] = new_id
            try:
                self._bus.publish(
                    self._Event(
                        ts=enriched["ts"],
                        kind=kind,
                        stable_id=safe_stable_id,
                        payload=enriched,
                        actor="voice",
                    )
                )
            except Exception:
                log.exception(
                    "StateBackedBus fanout failed for kind=%s id=%s; "
                    "events row committed but in-process subscribers "
                    "did not receive this event",
                    kind, new_id,
                )
        return new_id

    def recent(
        self, kind: str, limit: int = 10, *, actor: str | None = None
    ) -> list[dict]:
        # Phase 14 P14-F01: by default do NOT filter by actor so the voice
        # daemon can SEE events emitted by Phase 5/12 producers (READ_BPM,
        # RATE_TRACK, deck_state, etc.). Callers that explicitly want only
        # voice-published events can pass actor="voice".
        with self._lock:
            if actor is None:
                rows = self._conn.execute(
                    "SELECT id, payload_json FROM events "
                    "WHERE kind = ? "
                    "ORDER BY id DESC LIMIT ?",
                    (kind, int(limit)),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT id, payload_json FROM events "
                    "WHERE kind = ? AND actor = ? "
                    "ORDER BY id DESC LIMIT ?",
                    (kind, actor, int(limit)),
                ).fetchall()
        out: list[dict] = []
        for row in reversed(rows):
            try:
                payload = json.loads(row[1]) if row[1] else {}
            except json.JSONDecodeError:
                payload = {}
            payload["id"] = int(row[0])
            out.append(payload)
        return out

    def close(self) -> None:
        try:
            self._bus.close()
        finally:
            self._conn.close()


_WARNED_STUB = False


def make_bus(force_stub: bool = False, warn=print) -> EventBus:
    """Factory: :class:`StateBackedBus` when Phase 5 is importable,
    :class:`JsonlStubBus` otherwise.

    Phase 5 is shipped, so the expected path is ``StateBackedBus``. The
    fallback only trips when ``apps.shared.state`` cannot import or the
    state DB cannot be opened (e.g. read-only filesystem during a test
    sandbox), or when ``force_stub`` / ``--dry-bus`` is set.
    """
    global _WARNED_STUB
    if force_stub:
        return JsonlStubBus()
    try:
        return StateBackedBus()
    except (RuntimeError, ImportError, AttributeError, sqlite3.Error, OSError) as exc:
        if not _WARNED_STUB:
            warn(
                "[voice] Phase 5 state layer unavailable "
                f"({exc}); writing to JSONL stub bus at {voice_data_dir() / 'events.jsonl'}"
            )
            _WARNED_STUB = True
        return JsonlStubBus()


def reset_warning() -> None:
    """Test helper: reset the once-per-run warning latch."""
    global _WARNED_STUB
    _WARNED_STUB = False
