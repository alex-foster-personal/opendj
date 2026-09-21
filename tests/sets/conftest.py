"""Shared fixtures for Phase 12 set-recording tests."""
from __future__ import annotations

import sqlite3
import struct
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from apps.sets.state import SetsState

# ---------------------------------------------------------------------------
# state / paths
# ---------------------------------------------------------------------------


@pytest.fixture
def sets_root(tmp_path: Path) -> Path:
    """Isolated data/sets-like directory per test."""
    root = tmp_path / "sets"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def sets_state(tmp_path: Path) -> SetsState:
    """A fresh :class:`SetsState` on a temp DB path."""
    return SetsState(db_path=tmp_path / "sets.db")


# ---------------------------------------------------------------------------
# djay MediaLibrary fixture (minimal, handcrafted TSAF blobs)
# ---------------------------------------------------------------------------


def _tsaf_history_item_blob(
    *,
    uuid: str,
    session_uuid: str,
    title: str,
    artist: str,
    deck_number: int,
    start_time: float,
    duration: float,
) -> bytes:
    """Encode a minimal TSAF blob parseable by ``parse_history_item``.

    Mirrors the encoding hints from ``apps.sync.djay_monitor``:
      * strings:  ``0x08 <utf8 value> 0x00 ... 0x08 <field name> 0x00``
      * doubles:  ``<LE double> 0x08 <field name> 0x00``
      * uint8s:   ``0x0f <value> 0x08 <field name> 0x00``
    """
    parts: list[bytes] = []

    def string(value: str, name: str) -> None:
        parts.append(b"\x08" + value.encode("utf-8") + b"\x00")
        parts.append(b"\x08" + name.encode("utf-8") + b"\x00")

    def double(value: float, name: str) -> None:
        parts.append(struct.pack("<d", value))
        parts.append(b"\x08" + name.encode("utf-8") + b"\x00")

    def uint8(value: int, name: str) -> None:
        parts.append(b"\x0f" + bytes([value]))
        parts.append(b"\x08" + name.encode("utf-8") + b"\x00")

    string(uuid, "uuid")
    string(session_uuid, "sessionUUID")
    string(title, "title")
    string(artist, "artist")
    double(start_time, "startTime")
    double(duration, "duration")
    uint8(deck_number, "deckNumber")
    return b"".join(parts)


def _coredata_ts(wall: datetime) -> float:
    """Convert a wall clock datetime to a Core Data timestamp."""
    epoch = datetime(2001, 1, 1, tzinfo=UTC)
    return (wall - epoch).total_seconds()


def make_djay_fixture_db(path: Path, items: list[dict]) -> None:
    """Write a minimal MediaLibrary.db-shaped fixture at ``path``.

    ``items`` is a list of dicts with keys: uuid, session_uuid, title,
    artist, deck_number, start_time (datetime), duration.
    """
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            """
            CREATE TABLE database2 (
                collection TEXT NOT NULL,
                key        TEXT NOT NULL,
                data       BLOB NOT NULL
            )
            """
        )
        if items:
            # Insert one historySessions row per unique session_uuid.
            seen_sessions: set[str] = set()
            for it in items:
                if it["session_uuid"] in seen_sessions:
                    continue
                seen_sessions.add(it["session_uuid"])
                conn.execute(
                    "INSERT INTO database2(collection, key, data) VALUES (?, ?, ?)",
                    ("historySessions", it["session_uuid"], b""),
                )
        for it in items:
            blob = _tsaf_history_item_blob(
                uuid=it["uuid"],
                session_uuid=it["session_uuid"],
                title=it["title"],
                artist=it["artist"],
                deck_number=int(it["deck_number"]),
                start_time=_coredata_ts(it["start_time"]),
                duration=float(it["duration"]),
            )
            conn.execute(
                "INSERT INTO database2(collection, key, data) VALUES (?, ?, ?)",
                ("historySessionItems", it["uuid"], blob),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def djay_fixture_factory(tmp_path: Path):
    """Factory that writes a minimal djay fixture DB and returns its path."""

    def _factory(items: list[dict], *, name: str = "djay_fixture.db") -> Path:
        db_path = tmp_path / name
        make_djay_fixture_db(db_path, items)
        return db_path

    return _factory


@pytest.fixture
def djay_three_tracks_two_decks(tmp_path: Path) -> Path:
    """DB with 3 history items: deck A, deck B, deck A (two deck changes)."""
    start = datetime(2026, 4, 17, 21, 30, tzinfo=UTC)
    items = [
        {
            "uuid": f"uuid-{i}",
            "session_uuid": "session-1",
            "title": f"Track {i}",
            "artist": f"Artist {i}",
            "deck_number": deck,
            "start_time": start + timedelta(minutes=i * 3),
            "duration": 180.0,
        }
        for i, deck in enumerate([1, 2, 1], start=1)
    ]
    path = tmp_path / "djay_sample.db"
    make_djay_fixture_db(path, items)
    return path


# ---------------------------------------------------------------------------
# Rekordbox history fixture (minimal)
# ---------------------------------------------------------------------------


def make_rb_history_fixture(path: Path, items: list[dict]) -> None:
    """Minimal master.db fixture with a HISTORY playlist of ``items`` tracks."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            """
            CREATE TABLE djmdPlaylist (
                ID          TEXT PRIMARY KEY,
                Name        TEXT NOT NULL,
                Attribute   INTEGER NOT NULL DEFAULT 0,
                updated_at  TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE djmdSongPlaylist (
                ID          TEXT PRIMARY KEY,
                PlaylistID  TEXT NOT NULL,
                ContentID   TEXT,
                TrackNo     INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO djmdPlaylist(ID, Name, Attribute, updated_at)"
            " VALUES (?, ?, ?, ?)",
            ("hist-1", "HISTORY 2026-04-17", 1, "2026-04-17T20:00:00Z"),
        )
        for i, it in enumerate(items, start=1):
            conn.execute(
                "INSERT INTO djmdSongPlaylist(ID, PlaylistID, ContentID, TrackNo)"
                " VALUES (?, ?, ?, ?)",
                (f"sp-{i}", "hist-1", it["content_id"], i),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def rb_history_db(tmp_path: Path) -> Path:
    path = tmp_path / "master_with_history.db"
    make_rb_history_fixture(
        path,
        [
            {"content_id": "c-1"},
            {"content_id": "c-2"},
            {"content_id": "c-3"},
        ],
    )
    return path
