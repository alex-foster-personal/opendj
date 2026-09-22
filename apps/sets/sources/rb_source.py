"""Rekordbox HISTORY deck-state source.

Plan 12-01 Step 4. Tails the latest HISTORY playlist in ``master.db``
(working copy) and emits one ``track_loaded`` event per new row seen.

Rekordbox flushes HISTORY on track END, not start, so rb_source
events are inherently LATE relative to djay_source. The classifier
in Plan 12-02 is skew-tolerant; we document the lag here.

We do direct SQL (no pyrekordbox ORM) because HISTORY playlists are
simple and reading via sqlite3 over a per-test working copy avoids
the ORM's unlock requirement.
"""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from ..state import Event, SetsState

logger = logging.getLogger(__name__)

SOURCE_NAME = "rb_history"

# Rekordbox HISTORY playlists use Attribute = 1 (history folder) and
# their children are the dated HISTORY entries. The latest is the
# most recently updated row.
_LATEST_HISTORY_SQL = (
    "SELECT ID, Name FROM djmdPlaylist "
    "WHERE Name LIKE 'HISTORY %' OR Name = 'HISTORY' "
    "ORDER BY updated_at DESC, ID DESC LIMIT 1"
)

_HISTORY_ITEMS_SQL = (
    "SELECT ID, ContentID FROM djmdSongPlaylist "
    "WHERE PlaylistID = ? ORDER BY TrackNo ASC, ID ASC"
)


class RekordboxHistorySource:
    """Per-session wrapper around a RB master.db working copy.

    Stores a cursor on the last-seen djmdSongPlaylist row id per
    history playlist. Each new row produces a ``track_loaded`` event.
    """

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
        self.db_path = Path(db_path)
        self.session_started_at = session_started_at
        self._on_event_cb = on_event
        self._last_playlist_id: str | None = None
        self._last_song_id: str | None = None

    # ------------------------------------------------------------------

    def _rel_ts(self) -> float:
        delta = datetime.now(UTC) - self.session_started_at
        return max(0.0, delta.total_seconds())

    def _emit(self, event: Event) -> None:
        try:
            self.state.record_event(event)
        except Exception as exc:  # pragma: no cover -- guard
            logger.warning("rb_source: record_event failed: %s", exc)
        if self._on_event_cb is not None:
            try:
                self._on_event_cb(event)
            except Exception as exc:  # pragma: no cover -- guard
                logger.warning("rb_source: on_event callback raised: %s", exc)

    def _emit_error(self, exc: Exception) -> None:
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

    def _open_ro(self) -> sqlite3.Connection:
        """Open a read-only handle on the working copy."""
        if not self.db_path.exists():
            raise FileNotFoundError(
                f"Rekordbox master.db working copy not found: {self.db_path}"
            )
        uri = f"file:{self.db_path}?mode=ro"
        return sqlite3.connect(uri, uri=True)

    # ------------------------------------------------------------------

    def poll_once(self) -> None:
        """Single poll; emits ``track_loaded`` for any new HISTORY rows.

        Any exception becomes a ``source_error`` event; the caller
        keeps calling us.
        """
        try:
            conn = self._open_ro()
        except FileNotFoundError as exc:
            self._emit_error(exc)
            return
        try:
            row = conn.execute(_LATEST_HISTORY_SQL).fetchone()
            if row is None:
                return
            playlist_id, playlist_name = str(row[0]), str(row[1])
            if playlist_id != self._last_playlist_id:
                # New history (new set); reset cursor.
                self._last_playlist_id = playlist_id
                self._last_song_id = None
            items = conn.execute(_HISTORY_ITEMS_SQL, (playlist_id,)).fetchall()
        except sqlite3.DatabaseError as exc:
            self._emit_error(exc)
            return
        finally:
            conn.close()

        # Emit one ``track_loaded`` per new row. Rows already emitted
        # are skipped via the cursor.
        seen_cursor = self._last_song_id
        skip = seen_cursor is not None
        for song_pl_id, content_id in items:
            song_pl_id = str(song_pl_id)
            content_id = str(content_id) if content_id is not None else None
            if skip:
                if song_pl_id == seen_cursor:
                    skip = False
                continue
            self._emit(
                Event(
                    session_id=self.session_id,
                    timestamp_s=self._rel_ts(),
                    wall_clock=datetime.now(UTC).isoformat(
                        timespec="milliseconds"
                    ),
                    deck=None,  # rb history does not carry a deck label
                    track_stable_id=content_id,
                    action="track_loaded",
                    source=SOURCE_NAME,
                    value={
                        "playlist_id": playlist_id,
                        "playlist_name": playlist_name,
                        "song_playlist_id": song_pl_id,
                        "note": "RB HISTORY writes on track END; event is late",
                    },
                )
            )
            self._last_song_id = song_pl_id


__all__ = ["RekordboxHistorySource", "SOURCE_NAME"]
