"""Disposable loopback engine for launcher Rust integration tests.

Prints one JSON readiness line on stdout, then serves until SIGTERM.
Modes:
  with-db  -- SqliteBackend + one BPM smartlist (live GET /api/v1/smartlists)
  no-db    -- InMemoryBackend + missing state.db path (503 SMARTLISTS_DB_UNAVAILABLE)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
import json
import signal
import socket
from datetime import UTC, datetime
from typing import Annotated, Any

import uvicorn
from fastapi import Depends, Request

from apps.engine_core.app import ENGINE_VERSION, HEALTH_PATH, EngineHealthOut, _drop_or_raise
from apps.shared.pairings.schema_sql import ensure_phase08_tables
from apps.shared.state import db as state_db
from apps.smartlists.repo import SmartlistsRepo
from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend, StateBackend
from apps.webui.server.deps import get_read_state
from apps.webui.server.routes import smartlists as smartlists_routes
from apps.webui.server.routes.health import health as legacy_health
from apps.webui.server.sqlite_backend import SqliteBackend
from tests.waits import start_uvicorn_in_thread

_BPM_RULE: dict[str, Any] = {
    "op": "and",
    "children": [
        {"field": "bpm", "op": ">=", "value": 120},
        {"field": "bpm", "op": "<=", "value": 130},
    ],
}


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def _seed_state_db(path: Path) -> str:
    conn = state_db.open_rw(path, apply_schema=True)
    try:
        ensure_phase08_tables(conn)
        base = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)
        for i, (sid, title, bpm, genre, rating) in enumerate(
            [
                ("sl-track-001", "Midnight Drive", 124.0, "deep house", 4),
                ("sl-track-002", "Oxide", 128.0, "techno", 3),
            ]
        ):
            created = _iso(base.replace(day=1 + i))
            conn.execute(
                "INSERT INTO tracks (stable_id, stable_id_tier, title, "
                "artists_json, album, isrc, duration_ms, file_path, "
                "content_hash, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    sid,
                    "inferred",
                    title,
                    json.dumps(["Test Artist"]),
                    None,
                    None,
                    300000,
                    None,
                    None,
                    created,
                    created,
                ),
            )
            for fname, value in (
                ("bpm", bpm),
                ("genre", genre),
                ("rating", rating),
            ):
                conn.execute(
                    "INSERT INTO track_fields (stable_id, field_name, "
                    "value_json, source, confidence, modified_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (sid, fname, json.dumps(value), "manual", None, created),
                )
        smartlist_id = SmartlistsRepo(conn).create("120s", _BPM_RULE).id
        conn.commit()
        return smartlist_id
    finally:
        conn.close()


def _add_test_health_route(app: Any, boot_id: str) -> None:
    _drop_or_raise(app, HEALTH_PATH, "legacy health route")
    app.state.engine_boot_id = boot_id
    app.state.contract_rev = "test-contract-rev"

    @app.get(
        HEALTH_PATH,
        response_model=EngineHealthOut,
        tags=["health"],
        name="engine_health",
    )
    async def engine_health(
        request: Request,
        backend: Annotated[StateBackend, Depends(get_read_state)],
    ) -> EngineHealthOut:
        base = await legacy_health(request, backend)
        return EngineHealthOut(
            **base.model_dump(),
            contract_rev=app.state.contract_rev,
            engine_version=ENGINE_VERSION,
            boot_id=app.state.engine_boot_id,
        )


def _run_with_db(data_dir: Path, boot_id: str) -> tuple[int, str]:
    db_path = data_dir / "state.db"
    smartlist_id = _seed_state_db(db_path)
    backend = SqliteBackend(db_path)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    port = int(listener.getsockname()[1])
    app = create_app(
        backend=backend,
        mount_frontend=False,
        enable_cors=False,
        state_db_path=str(db_path),
    )
    app.include_router(smartlists_routes.router, prefix="/api/v1")
    _add_test_health_route(app, boot_id)
    server, thread = start_uvicorn_in_thread(
        uvicorn.Config(app, log_level="warning"), what="disposable engine", sockets=[listener]
    )
    lock_path = data_dir / ".engine.lock"
    lock_path.write_text(
        json.dumps(
            {
                "pid": 4242,
                "role": "opendj-engine",
                "host": "127.0.0.1",
                "port": port,
                "boot_id": boot_id,
            }
        ),
        encoding="utf-8",
    )

    def _shutdown(*_: object) -> None:
        server.should_exit = True

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    print(
        json.dumps(
            {
                "port": port,
                "lock_path": str(lock_path),
                "smartlist_id": smartlist_id,
                "boot_id": boot_id,
            }
        ),
        flush=True,
    )
    thread.join()
    return port, smartlist_id


def _run_no_db(data_dir: Path, boot_id: str) -> int:
    missing_db = data_dir / "missing-state.db"
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    port = int(listener.getsockname()[1])
    app = create_app(
        backend=InMemoryBackend(),
        mount_frontend=False,
        enable_cors=False,
        state_db_path=str(missing_db),
    )
    app.include_router(smartlists_routes.router, prefix="/api/v1")
    _add_test_health_route(app, boot_id)
    server, thread = start_uvicorn_in_thread(
        uvicorn.Config(app, log_level="warning"), what="disposable engine", sockets=[listener]
    )
    lock_path = data_dir / ".engine.lock"
    lock_path.write_text(
        json.dumps(
            {
                "pid": 4242,
                "role": "opendj-engine",
                "host": "127.0.0.1",
                "port": port,
                "boot_id": boot_id,
            }
        ),
        encoding="utf-8",
    )

    def _shutdown(*_: object) -> None:
        server.should_exit = True

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    print(
        json.dumps({"port": port, "lock_path": str(lock_path), "boot_id": boot_id}),
        flush=True,
    )
    thread.join()
    return port


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--mode", choices=("with-db", "no-db"), required=True)
    parser.add_argument("--boot-id", default="launcher-test-boot")
    args = parser.parse_args(argv)
    args.data_dir.mkdir(parents=True, exist_ok=True)
    if args.mode == "with-db":
        _run_with_db(args.data_dir, args.boot_id)
    else:
        _run_no_db(args.data_dir, args.boot_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
