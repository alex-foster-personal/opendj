"""Serve a deterministic real SQLite event store for browser acceptance.

This is a fixture host, not a mocked API: requests execute the production
``query_play_analytics`` contract against the canonical ``sets`` and
``set_events`` schema.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from apps.engine_core.account.api import entitlements_router
from apps.engine_core.build_info import MANIFEST_ENV, add_build_info_route
from apps.engine_core.update_channel import add_update_check_route
from apps.play_analytics.api import create_router
from apps.shared import platform_paths
from apps.shared.state.schema import apply_migrations
from apps.webui.server.models import PreflightOut
from apps.webui.server.preflight_checks import run_preflight
from apps.webui.server.routes import client_events as client_events_routes
from apps.webui.server.routes import cloudsync_status as cloudsync_status_routes
from apps.webui.server.routes import ui_prefs as ui_prefs_routes


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


def _seed_state_db(state_db_path: Path) -> None:
    """A real, minimal, schema-current state.db for the preflight boot gate.

    #1568: the app shell (and everything mounted inside it, including the
    update-check request this harness's own tests wait on) never renders
    until GET /api/v1/preflight answers status "pass". That route reads a
    real state.db, not this fixture's throwaway sets/events db (a different
    schema entirely), so it needs its own -- built the same way
    tests/webui/library_wheel_fixtures.py builds one: apply_migrations for a
    schema-current db, then one minimal track row. file_path is left NULL on
    purpose: audio-access reports "pending" for a track with no recorded
    location, which is honest and, per run_preflight, never blocks "pass".
    """
    state_db_path.parent.mkdir(parents=True, exist_ok=True)
    if state_db_path.exists():
        state_db_path.unlink()
    connection = sqlite3.connect(state_db_path)
    try:
        apply_migrations(connection)
        connection.execute(
            "INSERT INTO tracks (stable_id, stable_id_tier, title, artists_json, "
            "created_at, updated_at) VALUES (?, 'inferred', ?, ?, ?, ?)",
            (
                "e2e-fixture-track",
                "Fixture Track",
                json.dumps(["Fixture Artist"]),
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        connection.commit()
    finally:
        connection.close()


def create_app(db_path: Path) -> FastAPI:
    _seed_database(db_path)
    # <fixture-dir>/state/state.db, matching the real DATA_DIR/state/state.db
    # layout: cloudsync_status_routes derives its data dir as
    # state_db_path.parent.parent, so this shape is what keeps it inside the
    # throwaway fixture dir instead of walking out of it.
    state_db_path = db_path.parent / "state" / "state.db"
    _seed_state_db(state_db_path)
    app = FastAPI()
    # The page under test boots the whole app shell, so it calls the same
    # start-up endpoints the real client does. Mount the REAL routers rather
    # than hand-stubbing payloads: a hand-stub is what drifted here in the
    # first place, and a router that moves takes this harness with it.
    # data_dir keeps ui-prefs and client-events writing beside the throwaway
    # e2e db instead of the developer's MDT_DATA_DIR.
    app.state.data_dir = db_path.parent
    app.state.client_event_log_dir = db_path.parent
    app.state.state_db_path = state_db_path
    app.include_router(create_router(db_path=db_path))
    app.include_router(entitlements_router, prefix="/api/v1")
    app.include_router(ui_prefs_routes.router, prefix="/api/v1")
    app.include_router(client_events_routes.router, prefix="/api/v1")
    app.include_router(cloudsync_status_routes.router, prefix="/api/v1")
    # Real routes, not a hand-stub: a dev checkout has no OPENDJ_PAYLOAD_MANIFEST,
    # so build identity resolves from git and carries app_version from
    # tauri.conf.json. The public manifest is still unpublished, so
    # /update/check answers HTTP 200 with endpoint-refused rather than 502.
    # Hermetic by the same mechanism CI runs under. Strip the manifest env so a
    # Playwright server that inherits a real OPENDJ_PAYLOAD_MANIFEST from its
    # launcher still resolves a repo identity, not a live payload's.
    fixture_environ = {k: v for k, v in os.environ.items() if k != MANIFEST_ENV}
    add_build_info_route(app, environ=fixture_environ, repo_root=platform_paths.PROJECT_ROOT)
    add_update_check_route(app)

    # Real check logic (apps.webui.server.preflight_checks.run_preflight),
    # not a hand-stub -- the shipped route wrapper reads a module-level
    # rb_config.STATE_DB constant with no way to point it at this fixture's
    # db, so this calls the same production function directly instead of
    # reimplementing its checks (#1568).
    @app.get("/api/v1/preflight", response_model=PreflightOut)
    def preflight() -> PreflightOut:
        return run_preflight(state_db_path)

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
