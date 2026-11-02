"""Shared helpers for play_analytics tests (fixtures live in conftest.py)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.play_analytics.http import create_router


def add_opendj_plays(db_path: Path) -> None:
    """Append Open DJ own-deck plays carrying real dwell values.

    12s is an audition, 240s is a full play. Both are RECORDED; only the
    read-time filter separates them.
    """
    connection = sqlite3.connect(db_path)
    try:
        connection.executemany(
            """
            INSERT INTO events(
                session_id, timestamp_s, wall_clock, deck, track_stable_id,
                action, value_json, source
            ) VALUES (?, ?, ?, ?, ?, 'track_loaded', ?, 'opendj_decks')
            """,
            [
                (
                    "late-private",
                    100.0,
                    "2026-07-21T23:00:00+00:00",
                    "1",
                    "odj-audition",
                    '{"title":"Audition","artist":"A","audible_s":12.0,'
                    '"played_fraction":0.05,"threshold_s":60.0}',
                ),
                (
                    "late-private",
                    200.0,
                    "2026-07-21T23:10:00+00:00",
                    "2",
                    "odj-full",
                    '{"title":"Full","artist":"B","audible_s":240.0,'
                    '"played_fraction":1.0,"threshold_s":60.0}',
                ),
            ],
        )
        connection.commit()
    finally:
        connection.close()


@contextmanager
def play_analytics_test_client(db_path: Path) -> Iterator[TestClient]:
    """FastAPI test client bound to one play-analytics router instance."""
    app = FastAPI()
    app.include_router(create_router(db_path=db_path))
    with TestClient(app) as client:
        yield client
