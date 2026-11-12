"""Shared helpers for the test_stems_separate_wire test modules (split from test_stems_separate_wire.py)."""

from __future__ import annotations

import json
import os
import sqlite3
import wave
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.engine_core.app import _register_job_kinds
from apps.engine_core.config import EngineConfig
from apps.engine_core.jobs.api import router as jobs_router
from apps.engine_core.jobs.reap import WorkerIdentity
from apps.engine_core.jobs.runner import JobRunner, worker_argv
from apps.engine_core.jobs.store import JobStore
from apps.shared import platform_paths
from apps.shared.state import db as state_db
from apps.stems.api import router as plan_router
from apps.stems.job import JOB_KIND, TRANSPORT_ENV
from apps.stems.relay import contract as relay_api
from apps.webui.server.routes import ingest as ingest_routes
from apps.webui.server.stems_scope import pending_stems_scope
from tests.health_lights import fixtures as library_fx

REPO_ROOT = Path(__file__).resolve().parents[2]
WIRE_PATH = REPO_ROOT / "apps/webui/frontend/tests/unit/fixtures/stems-separate-wire.json"
WIRE: dict[str, Any] = json.loads(WIRE_PATH.read_text(encoding="utf-8"))

# The track the captured request names; the library below holds it.
TRACK_ID: str = WIRE["enqueue_track"]["request"]["payload"]["stable_ids"][0]
READY_ID = "track-with-bundle"
STAMP = "2026-10-15T00:00:00+00:00"
VOLATILE_JOB_FIELDS = ("id", "created_at", "owner_pid", "owner_boot_id")
# The engine stamps its own library on every stems row (#13583); the capture
# stores it as the frontend test's engine library, since the real one is a
# per-run tmp dir. Tests assert the live value is the engine's library.
CAPTURED_DATA_DIR = "/srv/opendj/data"


def _normalized(row: dict[str, Any]) -> dict[str, Any]:
    """The row minus what differs on every run, which is asserted separately."""
    for field in VOLATILE_JOB_FIELDS:
        assert isinstance(row[field], (str, int)), f"{field} missing or untyped"
    out = {key: value for key, value in row.items() if key not in VOLATILE_JOB_FIELDS}
    payload = out.get("payload")
    if isinstance(payload, dict) and "data_dir" in payload:
        assert isinstance(payload["data_dir"], str) and Path(payload["data_dir"]).is_absolute()
        out["payload"] = {**payload, "data_dir": CAPTURED_DATA_DIR}
    return out


def _write_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x00" * 800)


@pytest.fixture
def shipped_build_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured relay build: the transport a shipped app separates through."""
    monkeypatch.delenv(TRANSPORT_ENV, raising=False)
    monkeypatch.setenv(relay_api.RELAY_BASE_ENV, "https://relay.example.invalid")
    monkeypatch.setenv(relay_api.IDENTITY_TOKEN_ENV, "test-identity-token")


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """A real, migrated state.db: one NO STEMS track with audio, one with a bundle."""
    cfg = EngineConfig(data_dir=tmp_path / "data")
    cfg.state_dir.mkdir(parents=True)
    state_db.open_rw(cfg.state_db).close()
    audio = tmp_path / "music" / "a.wav"
    ready_audio = tmp_path / "music" / "b.wav"
    _write_wav(audio)
    _write_wav(ready_audio)
    library_fx.write_stem_bundle(cfg.state_dir / "stems", READY_ID)
    connection = sqlite3.connect(cfg.state_db)
    try:
        connection.executemany(
            "INSERT INTO tracks (stable_id, stable_id_tier, title, file_path, duration_ms, "
            "created_at, updated_at) VALUES (?, 'inferred', ?, ?, 100, ?, ?)",
            [
                (sid, title, str(path), STAMP, STAMP)
                for sid, title, path in ((TRACK_ID, "A", audio), (READY_ID, "B", ready_audio))
            ],
        )
        connection.commit()
    finally:
        connection.close()
    return cfg.data_dir


def _build_app(jobs_db: Path, data_dir: Path, boot_id: str) -> tuple[FastAPI, JobStore]:
    """The engine's jobs and plan routers over a real store, runner not started
    (nothing claims a row out from under an assertion)."""
    _register_job_kinds()
    store = JobStore(jobs_db, boot_id=boot_id, owner_pid=os.getpid())
    store.recover()
    app = FastAPI()
    app.include_router(jobs_router, prefix="/api/v1")
    app.include_router(plan_router, prefix="/api/v1")
    app.state.jobs_store = store
    app.state.jobs_runner = JobRunner(store)
    app.state.engine_cfg = EngineConfig(data_dir=data_dir)
    # STEM-58: the composition root mounts the enrich card's stems scope
    # (apps.webui.server.app_wiring._bind_stem_and_usage); the jobs API
    # resolves ``scope: pending`` and the plan counts songs through it.
    app.state.stem_roots = (EngineConfig(data_dir=data_dir).state_dir / "stems",)
    app.state.stems_scope_fn = lambda: pending_stems_scope(app)
    return app, store


@pytest.fixture
def engine_library(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The engine's own library IS ``data_dir``, as on an installed app.

    The engine resolves its library from ``MDT_DATA_DIR`` (ingest's coverage
    globals are derived from it at import), and a ``scope: pending`` request
    is refused unless the data dir stamped on it is that library (#13583).
    So the coverage globals are pointed at this private tmp library, never
    the repo's ``data/``.
    """
    state = EngineConfig(data_dir=data_dir).state_dir
    monkeypatch.setenv(platform_paths.DATA_DIR_ENV, str(data_dir))
    monkeypatch.setattr(ingest_routes, "COVERAGE_DATA_DIR", data_dir)
    monkeypatch.setattr(ingest_routes, "DEFAULT_STEMS_DIR", state / "stems")
    monkeypatch.setattr(ingest_routes, "VOCAL_CACHE_DIR", state / "vocal-cache")
    monkeypatch.setattr(ingest_routes, "LYRICS_CACHE_DIR", state / "lyrics-cache")
    monkeypatch.setattr(ingest_routes, "CONFIG_PATH", state / "ingest-config.json")
    monkeypatch.setattr(ingest_routes, "open_ro", lambda: sqlite3.connect(state / "state.db"))
    return data_dir


@pytest.fixture
def client(shipped_build_env: None, engine_library: Path, tmp_path: Path) -> Iterator[TestClient]:
    app, store = _build_app(tmp_path / "jobs.db", engine_library, "boot-wire")
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        store.close()


def _post_from_own_engine(jobs_db: str, data_dir: str, request: dict[str, Any], go: Any, out: Any) -> None:
    """One process = one engine connection to the shared jobs.db, posting
    through its own real router the moment ``go`` is set."""
    app, store = _build_app(Path(jobs_db), Path(data_dir), f"boot-{os.getpid()}")
    try:
        with TestClient(app) as test_client:
            go.wait(60)
            response = test_client.post("/api/v1/jobs", json=request)
            out.put((response.status_code, response.json()))
    finally:
        store.close()


def _claimed_ids(store: JobStore) -> list[str]:
    """Ids of the rows the runner would claim next. ``JobStore.claim_queued``'s
    return annotation sits in a class that defines a ``list`` method, which
    mypy resolves it to, so the rows are typed here."""
    rows: list[dict[str, Any]] = store.claim_queued()
    return [row["id"] for row in rows]


def _ended(client: TestClient, request: dict[str, Any], status: str = "failed") -> dict[str, Any]:
    """Enqueue through the router, then end the row the way the runner does."""
    row = client.post("/api/v1/jobs", json=request).json()
    store: JobStore = client.app.state.jobs_store  # type: ignore[attr-defined]
    assert _claimed_ids(store) == [row["id"]]
    return store.finish(row["id"], status, error="boom")


def _scope_moved_to(client: TestClient, status: str) -> dict[str, Any]:
    store: JobStore = client.app.state.jobs_store  # type: ignore[attr-defined]
    scope = client.post("/api/v1/jobs", json=WIRE["enqueue_scope"]["request"]).json()
    assert scope["status"] == "queued"
    if status in ("running", "cancelling"):
        assert _claimed_ids(store) == [scope["id"]]
    if status == "cancelling":
        store.begin_cancel(scope["id"])
    assert store.get(scope["id"])["status"] == status
    return scope


def _track_request(*ids: str) -> dict[str, Any]:
    return {"kind": JOB_KIND, "payload": {"stable_ids": list(ids), "tier": "M"}}


def _at_tier(request: dict[str, Any], tier: str) -> dict[str, Any]:
    return {**request, "payload": {**request["payload"], "tier": tier}}


def _launch(store: JobStore, status: str) -> dict[str, Any]:
    """Claim the next queued row and record the argv its worker would run
    with now, as ``JobRunner._run`` does at spawn; no process is started."""
    (job_id,) = _claimed_ids(store)
    argv = worker_argv(JOB_KIND, store.get(job_id)["payload"])
    # A pgid no process holds: nothing here reaps it, and a kill would miss.
    store.record_worker(
        job_id, pid=2**22 - 1, identity=WorkerIdentity(pgid=2**22 - 1, argv=tuple(argv), started_at=0.0)
    )
    if status == "cancelling":
        store.begin_cancel(job_id)
    row = store.get(job_id)
    assert row["status"] == status
    return row


def _in_library(request: dict[str, Any], data_dir: str) -> dict[str, Any]:
    return {**request, "payload": {**request["payload"], "data_dir": data_dir}}
