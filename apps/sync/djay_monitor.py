"""djay Pro now-playing monitor.

Ported from ``~/Desktop/music-dj/src/monitor.py`` per Phase 2 D1 "port
as-is, then refactor". Polls djay Pro's MediaLibrary.db history_session
rows, decodes TSAF blobs, and emits now-playing events via a callback.

Behaviour identical to the companion; only the import path changed.

Requirements:
- [if] djay Pro DB has history sessions [then] latest session is returned
- [if] history session has items [then] items include title, artist, deck number
- [if] new track appears since last poll [then] on_new_track callback fires
- [if] no new tracks since last poll [then] no callback
- [if] DB not available [then] raises FileNotFoundError
"""

from __future__ import annotations

import sqlite3
import struct
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from apps.shared.paths import DJAY_LIVE_DB

# Core Data epoch: 2001-01-01 00:00:00 UTC
COREDATA_EPOCH = datetime(2001, 1, 1, tzinfo=UTC)

# Default DB path (live). Tests always pass an explicit path; this
# default is only for manual runs via ``python -m apps.sync.djay_monitor``.
DEFAULT_DB_PATH = DJAY_LIVE_DB


def coredata_timestamp_to_datetime(timestamp: float | None) -> datetime | None:
    """Convert Core Data timestamp (seconds since 2001-01-01) to datetime."""
    if timestamp is None:
        return None
    return COREDATA_EPOCH + timedelta(seconds=timestamp)


@dataclass
class HistoryItem:
    """A single track play event from djay Pro history."""

    uuid: str
    session_uuid: str
    title: str
    artist: str
    deck_number: int | None
    start_time: float | None  # Core Data timestamp
    duration: float | None

    @property
    def start_datetime(self) -> datetime | None:
        return coredata_timestamp_to_datetime(self.start_time)


def _extract_tsaf_strings(data: bytes) -> list[str]:
    """Extract 0x08-prefixed null-terminated strings from a TSAF blob."""
    strings: list[str] = []
    i = 0
    while i < len(data) - 1:
        if data[i] == 0x08:
            end = data.find(0x00, i + 1)
            if end > 0 and end - i < 500:
                try:
                    s = data[i + 1 : end].decode("utf-8")
                    if s:
                        strings.append(s)
                except UnicodeDecodeError:
                    pass
            i = end + 1 if end > 0 else i + 1
        else:
            i += 1
    return strings


def _extract_field_double(data: bytes, field_name: str) -> float | None:
    """Extract LE double value for a named TSAF field.

    TSAF stores the 8-byte LE double immediately before the field name
    marker. Pattern: <8-byte LE double> 0x08 <field_name> 0x00
    """
    marker = b"\x08" + field_name.encode("utf-8") + b"\x00"
    idx = data.find(marker)
    if idx < 8:
        return None
    try:
        return struct.unpack("<d", data[idx - 8 : idx])[0]
    except struct.error:
        return None


def _extract_field_uint8(data: bytes, field_name: str) -> int | None:
    """Extract uint8 value for a named TSAF field.

    Pattern: 0x0f <uint8> 0x08 <field_name> 0x00.
    """
    marker = b"\x08" + field_name.encode("utf-8") + b"\x00"
    idx = data.find(marker)
    if idx < 2:
        return None
    if data[idx - 2] == 0x0F:
        return data[idx - 1]
    return None


def parse_history_item(data: bytes, key: str) -> HistoryItem:
    """Parse a historySessionItems TSAF blob into HistoryItem."""
    strings = _extract_tsaf_strings(data)

    uuid = ""
    session_uuid = ""
    title = ""
    artist = ""
    for i, s in enumerate(strings):
        if s == "uuid" and i > 0:
            uuid = strings[i - 1]
        elif s == "sessionUUID" and i > 0:
            session_uuid = strings[i - 1]
        elif s == "title" and i > 0:
            title = strings[i - 1]
        elif s == "artist" and i > 0:
            artist = strings[i - 1]

    start_time = _extract_field_double(data, "startTime")
    duration = _extract_field_double(data, "duration")
    deck_number = _extract_field_uint8(data, "deckNumber")

    return HistoryItem(
        uuid=uuid or key,
        session_uuid=session_uuid,
        title=title,
        artist=artist,
        deck_number=deck_number,
        start_time=start_time,
        duration=duration,
    )


def get_latest_session_items(db_path: Path = DEFAULT_DB_PATH) -> list[HistoryItem]:
    """Read history items from the most recent djay Pro session.

    Returns items sorted by start_time ascending. Raises
    :class:`FileNotFoundError` if the DB is missing.
    """
    if not Path(db_path).exists():
        raise FileNotFoundError(f"djay Pro database not found: {db_path}")

    conn = sqlite3.connect(str(db_path))
    try:
        session_rows = conn.execute(
            "SELECT key, data FROM database2 WHERE collection='historySessions'"
        ).fetchall()
        if not session_rows:
            return []
        item_rows = conn.execute(
            "SELECT key, data FROM database2 WHERE collection='historySessionItems'"
        ).fetchall()
        if not item_rows:
            return []

        all_items: list[HistoryItem] = []
        for key, data in item_rows:
            try:
                item = parse_history_item(data, key)
                all_items.append(item)
            except Exception:
                continue

        all_items.sort(key=lambda x: x.start_time or 0.0)

        if not all_items:
            return []

        latest_session = all_items[-1].session_uuid
        if latest_session:
            session_items = [
                i for i in all_items if i.session_uuid == latest_session
            ]
            session_items.sort(key=lambda x: x.start_time or 0.0)
            return session_items

        return all_items[-20:]
    finally:
        conn.close()


def get_now_playing(items: list[HistoryItem]) -> HistoryItem | None:
    """Return the most recently played track from ``items``."""
    if not items:
        return None
    return items[-1]


class DjayNowPlaying:
    """Stateful monitor that detects track changes in djay Pro.

    Usage::

        def on_track(item: HistoryItem):
            print(f"Now playing: {item.title} by {item.artist}")

        monitor = DjayNowPlaying(on_new_track=on_track)
        monitor.poll()
    """

    def __init__(
        self,
        on_new_track: Callable[[HistoryItem], None],
        db_path: Path = DEFAULT_DB_PATH,
    ) -> None:
        self.on_new_track = on_new_track
        self.db_path = db_path
        self._last_uuid: str | None = None
        self.history: list[HistoryItem] = []
        self._current_track: HistoryItem | None = None

    @property
    def current_track(self) -> HistoryItem | None:
        return self._current_track

    def _process_now_playing(self, item: HistoryItem | None) -> None:
        if item is None:
            return
        if item.uuid != self._last_uuid:
            self._last_uuid = item.uuid
            self._current_track = item
            self.history.append(item)
            self.on_new_track(item)

    def poll(self) -> HistoryItem | None:
        """Poll the DB for current track. Returns the latest item."""
        items = get_latest_session_items(self.db_path)
        now = get_now_playing(items)
        self._process_now_playing(now)
        return now


def _cli() -> int:
    import argparse

    p = argparse.ArgumentParser(description="djay Pro now-playing inspector")
    p.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    p.add_argument(
        "--poll-once",
        action="store_true",
        help="print latest session items and exit",
    )
    args = p.parse_args()
    try:
        items = get_latest_session_items(args.db)
    except FileNotFoundError as exc:
        print(f"error: {exc}")
        return 2
    for item in items:
        print(
            f"{item.start_datetime}  deck={item.deck_number}  "
            f"{item.artist} - {item.title}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())


__all__ = [
    "COREDATA_EPOCH",
    "DEFAULT_DB_PATH",
    "HistoryItem",
    "coredata_timestamp_to_datetime",
    "parse_history_item",
    "get_latest_session_items",
    "get_now_playing",
    "DjayNowPlaying",
]
