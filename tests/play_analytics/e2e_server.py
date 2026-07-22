"""Serve a deterministic real SQLite event store for browser acceptance.

This is a fixture host, not a mocked API: requests execute the production
``query_play_analytics`` contract against the canonical ``sets`` and
``set_events`` schema.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from apps.play_analytics.api import create_router


def _seed_database(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    with sqlite3.connect(db_path) as connection:
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
            CREATE TABLE events (
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
            "INSERT INTO sets VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    "warehouse-2026-07-21",
                    "2026-07-21T22:00:00+00:00",
                    None,
                    "BlackHole 2ch",
                    "private",
                    None,
                ),
                (
                    "studio-2026-07-20",
                    "2026-07-20T20:00:00+00:00",
                    "2026-07-20T21:30:00+00:00",
                    "BlackHole 2ch",
                    "shared_local",
                    "Warm-up set",
                ),
            ],
        )
        rows = [
            (
                "studio-2026-07-20",
                10.0,
                "2026-07-20T20:00:10+00:00",
                "A",
                "track-a",
                "Alpha",
                "Artist One",
            ),
            (
                "studio-2026-07-20",
                200.0,
                "2026-07-20T20:03:20+00:00",
                "B",
                "track-b",
                "Beta",
                "Artist Two",
            ),
            (
                "warehouse-2026-07-21",
                5.0,
                "2026-07-21T22:00:05+00:00",
                "A",
                "track-a",
                "Alpha",
                "Artist One",
            ),
            (
                "warehouse-2026-07-21",
                190.0,
                "2026-07-21T22:03:10+00:00",
                "B",
                "track-c",
                "Gamma",
                "Artist Three",
            ),
        ]
        connection.executemany(
            """
            INSERT INTO events(
                session_id, timestamp_s, wall_clock, deck, track_stable_id,
                action, value_json, source
            ) VALUES (?, ?, ?, ?, ?, 'track_loaded', ?, 'e2e_fixture')
            """,
            [
                (*row[:5], json.dumps({"title": row[5], "artist": row[6]}))
                for row in rows
            ],
        )


def create_app(db_path: Path) -> FastAPI:
    _seed_database(db_path)
    app = FastAPI()
    app.include_router(create_router(db_path=db_path))

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ready"}

    @app.get("/api/v1/health")
    def app_health() -> dict[str, object]:
        return {
            "status": "ok",
            "state_db": {
                "path": str(db_path),
                "tracks": 3,
                "playlists": 0,
                "pairings": 0,
            },
            "cloud": {"lock_holder": None},
            "syncthing": None,
            "bind_host": "127.0.0.1",
            "version": "e2e-fixture",
        }

    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9414)
    args = parser.parse_args()
    uvicorn.run(create_app(args.db), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
