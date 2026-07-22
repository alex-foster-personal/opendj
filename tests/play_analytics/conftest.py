"""Deterministic Phase 12 event-store fixtures for play analytics."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def analytics_db(tmp_path: Path) -> Path:
    """Create a real canonical ``sets`` + ``events`` SQLite database."""
    db_path = tmp_path / "sets.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(
            """
            CREATE TABLE sets (
                session_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                capture_device TEXT NOT NULL,
                share_state TEXT NOT NULL,
                notes TEXT
            );
            CREATE TABLE set_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sets(session_id),
                timestamp_s REAL NOT NULL,
                wall_clock TEXT NOT NULL,
                deck TEXT,
                track_stable_id TEXT,
                action TEXT NOT NULL,
                value_json TEXT,
                source TEXT NOT NULL
            );
            """
        )
        connection.executemany(
            """
            INSERT INTO sets(
                session_id, started_at, ended_at, capture_device, share_state, notes
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "late-private",
                    "2026-07-21T22:00:00+00:00",
                    None,
                    "BlackHole 2ch",
                    "private",
                    None,
                ),
                (
                    "early-shared",
                    "2026-07-20T20:00:00+00:00",
                    "2026-07-20T21:30:00+00:00",
                    "BlackHole 2ch",
                    "shared_local",
                    "Warm-up set",
                ),
            ],
        )
        events = [
            (
                "early-shared",
                10.0,
                "2026-07-20T20:00:10+00:00",
                "A",
                "track-a",
                "track_loaded",
                {"title": "Alpha", "artist": "Artist One"},
                "djay_monitor",
            ),
            (
                "early-shared",
                200.0,
                "2026-07-20T20:03:20+00:00",
                "B",
                "track-b",
                "track_loaded",
                {"title": "Beta", "artist": "Artist Two"},
                "djay_monitor",
            ),
            (
                "early-shared",
                205.0,
                "2026-07-20T20:03:25+00:00",
                "B",
                "track-b",
                "tempo_changed",
                {"bpm": 124.0},
                "djay_monitor",
            ),
            (
                "late-private",
                5.0,
                "2026-07-21T22:00:05+00:00",
                "A",
                "track-a",
                "track_loaded",
                {"title": "Alpha", "artist": "Artist One"},
                "rekordbox_history",
            ),
            (
                "late-private",
                190.0,
                "2026-07-21T22:03:10+00:00",
                "B",
                "track-c",
                "track_loaded",
                {"title": "Gamma", "artist": "Artist Three"},
                "djay_monitor",
            ),
        ]
        connection.executemany(
            """
            INSERT INTO set_events(
                session_id, timestamp_s, wall_clock, deck, track_stable_id,
                action, value_json, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [(*event[:6], json.dumps(event[6]), event[7]) for event in events],
        )
        connection.commit()
    finally:
        connection.close()
    return db_path
