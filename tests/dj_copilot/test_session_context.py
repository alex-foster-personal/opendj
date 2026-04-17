"""SessionContext loader tests (AI-01)."""
from __future__ import annotations

import sqlite3

import pytest

from apps.dj_copilot.session_context import (
    PlayedTrack,
    SessionContext,
    load_session_context,
)
from tests.dj_copilot.conftest import FIXED_NOW

pytestmark = pytest.mark.requirement("AI-01")


def _seed_phase12_events(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS session_events (
            stable_id    TEXT NOT NULL,
            artist       TEXT,
            bpm          REAL,
            key_camelot  TEXT,
            energy       INTEGER,
            played_at    TEXT NOT NULL,
            action       TEXT NOT NULL
        )
        """
    )


def test_manual_source(tmp_path) -> None:
    tracks = [
        PlayedTrack(
            stable_id="t-1",
            artist="A",
            bpm=120.0,
            key_camelot="8A",
            energy=5,
            played_at=FIXED_NOW,
        )
    ]
    ctx = load_session_context(source="manual", manual=tracks)
    assert ctx.source == "manual"
    assert [t.stable_id for t in ctx.recent] == ["t-1"]


def test_empty_when_no_sources(tmp_path) -> None:
    db = tmp_path / "s.db"
    conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        ctx = load_session_context(conn=conn, source="auto")
        assert ctx.source == "empty"
        assert ctx.recent == []
    finally:
        conn.close()


def test_phase12_source(tmp_path) -> None:
    db = tmp_path / "s.db"
    conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        _seed_phase12_events(conn)
        now = FIXED_NOW.isoformat()
        conn.executemany(
            "INSERT INTO session_events"
            "(stable_id, artist, bpm, key_camelot, energy, played_at, action) "
            "VALUES (?, ?, ?, ?, ?, ?, 'now_playing')",
            [
                (f"t-{i}", f"A{i}", 120.0 + i, "8A", 5, now)
                for i in range(3)
            ],
        )
        ctx = load_session_context(conn=conn, source="phase12", limit=12)
        assert ctx.source == "phase12"
        assert len(ctx.recent) == 3
    finally:
        conn.close()


def test_auto_prefers_phase12(tmp_path) -> None:
    db = tmp_path / "s.db"
    conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        _seed_phase12_events(conn)
        conn.execute(
            "INSERT INTO session_events"
            "(stable_id, artist, bpm, key_camelot, energy, played_at, action) "
            "VALUES (?, ?, ?, ?, ?, ?, 'now_playing')",
            ("t-a", "A", 120.0, "8A", 5, FIXED_NOW.isoformat()),
        )
        ctx = load_session_context(conn=conn, source="auto")
        assert ctx.source == "phase12"
    finally:
        conn.close()


def test_ignores_non_now_playing(tmp_path) -> None:
    db = tmp_path / "s.db"
    conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        _seed_phase12_events(conn)
        conn.execute(
            "INSERT INTO session_events"
            "(stable_id, artist, bpm, key_camelot, energy, played_at, action) "
            "VALUES (?, ?, ?, ?, ?, ?, 'skipped')",
            ("t-x", "X", 120.0, "8A", 5, FIXED_NOW.isoformat()),
        )
        ctx = load_session_context(conn=conn, source="phase12")
        assert ctx.recent == []
        # P13-F02: an explicit source request returning an empty result
        # must preserve the requested source label so callers can tell
        # the request was honored but yielded no rows.
        assert ctx.source == "phase12"
    finally:
        conn.close()
