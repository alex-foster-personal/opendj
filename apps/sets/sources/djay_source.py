"""djay deck-state source.

Wraps :class:`apps.sync.djay_monitor.DjayNowPlaying` and translates its
"new history item" callback into timeline events. Emits two event
kinds per CONTEXT D3::

    action = "track_loaded"  -- first time this history UUID is seen
    action = "track_change"  -- the deck assignment differs from the
                                previous track_loaded on another deck

Poll cadence matches the CONTEXT D2 budget (500 ms); the caller
drives the loop.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from apps.sync.djay_monitor import DjayNowPlaying, HistoryItem

from ..state import Event, SetsState

logger = logging.getLogger(__name__)

SOURCE_NAME = "djay_monitor"


class DjaySource:
    """Per-session wrapper. One instance per live recorder."""

    def __init__(
        self,
        session_id: str,
        state: SetsState,
        *,
        db_path: Path,
        session_started_at: datetime,
        on_event: Callable[[Event], None] | None = None,
    ) -> None:
        self.session_id = session_id
        self.state = state
        self.session_started_at = session_started_at
        self._on_event_cb = on_event
        self._last_deck: str | None = None
        self._last_uuid: str | None = None

        # DjayNowPlaying fires the callback on every new uuid seen.
        self._monitor = DjayNowPlaying(
            on_new_track=self._handle_track,
            db_path=db_path,
        )

    # ------------------------------------------------------------------

    def _deck_label(self, item: HistoryItem) -> str | None:
        if item.deck_number is None:
            return None
        # djay's deck_number is 1-based (1 -> "A", 2 -> "B", ...).
        mapping = {1: "A", 2: "B", 3: "C", 4: "D"}
        return mapping.get(int(item.deck_number), str(item.deck_number))

    def _rel_ts(self) -> float:
        delta = datetime.now(UTC) - self.session_started_at
        return max(0.0, delta.total_seconds())

    def _emit(self, event: Event) -> None:
        try:
            self.state.record_event(event)
        except Exception as exc:  # pragma: no cover -- guard
            logger.warning("djay_source: record_event failed: %s", exc)
        if self._on_event_cb is not None:
            try:
                self._on_event_cb(event)
            except Exception as exc:  # pragma: no cover -- guard
                logger.warning("djay_source: on_event callback raised: %s", exc)

    def _handle_track(self, item: HistoryItem) -> None:
        deck = self._deck_label(item)
        wall = datetime.now(UTC).isoformat(timespec="milliseconds")
        ts = self._rel_ts()
        stable_id = item.uuid or None
        base_value = {
            "title": item.title,
            "artist": item.artist,
            "uuid": item.uuid,
            "duration_s": item.duration,
        }

        # Always emit track_loaded for a new uuid.
        if item.uuid != self._last_uuid:
            loaded = Event(
                session_id=self.session_id,
                timestamp_s=ts,
                wall_clock=wall,
                deck=deck,
                track_stable_id=stable_id,
                action="track_loaded",
                source=SOURCE_NAME,
                value=base_value,
            )
            self._emit(loaded)
            # track_change when the active deck differs from the last one.
            if (
                self._last_deck is not None
                and deck is not None
                and deck != self._last_deck
            ):
                change = Event(
                    session_id=self.session_id,
                    timestamp_s=ts,
                    wall_clock=wall,
                    deck=deck,
                    track_stable_id=stable_id,
                    action="track_change",
                    source=SOURCE_NAME,
                    value={
                        "from_deck": self._last_deck,
                        "to_deck": deck,
                        "from_uuid": self._last_uuid,
                        "to_uuid": item.uuid,
                    },
                )
                self._emit(change)
            self._last_uuid = item.uuid
            if deck is not None:
                self._last_deck = deck

    # ------------------------------------------------------------------

    def poll_once(self) -> None:
        """Single poll; emits zero or more events.

        Any exception is caught and recorded as a ``source_error`` event
        so the recorder stays alive.
        """
        try:
            self._monitor.poll()
        except FileNotFoundError as exc:
            self._emit(
                Event(
                    session_id=self.session_id,
                    timestamp_s=self._rel_ts(),
                    wall_clock=datetime.now(UTC).isoformat(
                        timespec="milliseconds"
                    ),
                    action="source_error",
                    source=SOURCE_NAME,
                    value={"kind": "FileNotFoundError", "detail": str(exc)},
                )
            )
        except Exception as exc:  # pragma: no cover -- guard
            self._emit(
                Event(
                    session_id=self.session_id,
                    timestamp_s=self._rel_ts(),
                    wall_clock=datetime.now(UTC).isoformat(
                        timespec="milliseconds"
                    ),
                    action="source_error",
                    source=SOURCE_NAME,
                    value={"kind": type(exc).__name__, "detail": str(exc)},
                )
            )


__all__ = ["DjaySource", "SOURCE_NAME"]
