"""Shared app fixture: real state.db, real routers, tmp artifact dirs."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.webui.server.routes import coverage_drain as drain_routes
from apps.webui.server.routes import coverage_terminal as terminal_routes
from apps.webui.server.routes import ingest as ingest_mod
from apps.webui.server.routes import ingest_job
from tests.health_lights import fixtures as fx


@dataclass
class Library:
    app: FastAPI
    client: TestClient
    data_dir: Path
    state_db: Path
    music: Path
    stems: Path
    vocal_cache: Path
    lyrics_cache: Path


@pytest.fixture
def library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Library]:
    data_dir = tmp_path / "data"
    state_db = fx.make_state_db(data_dir)
    state = data_dir / "state"
    monkeypatch.setenv("MDT_DATA_DIR", str(data_dir))
    monkeypatch.setattr(ingest_mod, "CONFIG_PATH", state / "ingest-config.json")
    monkeypatch.setattr(ingest_mod, "VOCAL_CACHE_DIR", state / "vocal-cache")
    monkeypatch.setattr(ingest_mod, "LYRICS_CACHE_DIR", state / "lyrics-cache")
    monkeypatch.setattr(ingest_mod, "DEFAULT_STEMS_DIR", state / "stems")
    monkeypatch.setattr(ingest_mod, "COVERAGE_DATA_DIR", data_dir)
    monkeypatch.setattr(ingest_mod, "open_ro", lambda: sqlite3.connect(state_db))
    ingest_job.BUNDLE_CACHE.clear()

    app = FastAPI()
    app.include_router(ingest_mod.router, prefix="/api/v1")
    app.include_router(drain_routes.router, prefix="/api/v1")
    app.include_router(terminal_routes.router, prefix="/api/v1")
    app.state.state_db = state_db
    app.state.stem_roots = (state / "stems",)
    # The farm is reachable in these tests unless a test says otherwise: a
    # plain value on the app, the same seam as ``stem_roots``.
    app.state.stems_source_refusal_fn = lambda: None
    with TestClient(app) as client:
        yield Library(
            app=app,
            client=client,
            data_dir=data_dir,
            state_db=state_db,
            music=tmp_path / "music",
            stems=state / "stems",
            vocal_cache=state / "vocal-cache",
            lyrics_cache=state / "lyrics-cache",
        )
