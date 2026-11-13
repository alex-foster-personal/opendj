"""Browser-posted control-event v2 ingest (SETREC-11827)."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from ..control_events import ControlEventV2, append_control_jsonl, parse_control_event_v2
from ..state import SetsState


class ControlEventIngestError(ValueError):
    """Raised when a batch cannot be ingested."""


class ControlEventSource:
    def __init__(
        self,
        *,
        session_id: str,
        state: SetsState,
        session_dir: Path,
    ) -> None:
        self.session_id = session_id
        self._state = state
        self._session_dir = session_dir
        self._lock = threading.Lock()
        self._next_seq: int | None = None

    def _expected_seq(self, seen: int) -> int:
        with self._lock:
            if self._next_seq is None:
                last = self._state.max_control_seq(self.session_id)
                self._next_seq = 0 if last is None else last + 1
            if seen != self._next_seq:
                raise ControlEventIngestError(f"control event seq gap: expected {self._next_seq}, got {seen}")
            self._next_seq += 1
            return seen

    def submit_many(self, events: list[dict[str, Any]]) -> int:
        accepted = 0
        for raw in events:
            try:
                event = parse_control_event_v2(raw)
            except ValueError as exc:
                raise ControlEventIngestError(str(exc)) from exc
            if event.session_id != self.session_id:
                raise ControlEventIngestError(f"control event session_id {event.session_id!r} != {self.session_id!r}")
            self._expected_seq(event.seq)
            self._state.record_control_event(event)
            append_control_jsonl(self._session_dir, event)
            accepted += 1
        return accepted
