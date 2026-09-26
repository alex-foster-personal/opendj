"""Setup import completion must invalidate the library over the events bus."""

from __future__ import annotations

import os
import struct
import time
import wave
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.engine_core.config import EngineConfig
from apps.engine_core.jobs.api import router as jobs_router
from apps.engine_core.jobs.runner import JobRunner, register_progress_observer
from apps.engine_core.jobs.store import JobStore
from apps.engine_core.setup import detect
from apps.engine_core.setup.api import router as setup_router
from apps.engine_core.setup.jobs import SETUP_IMPORT_KIND
from apps.engine_core.setup.library_events import on_setup_import_progress
from apps.shared.events import set_hub
from tests.engine_core.conftest import build_identity
from tests.engine_core.test_emit_points import RecordingHub
from tests.engine_core.test_setup_worker import JOB_DEADLINE_S, POLL_S

TRACKS_INVALIDATION = ("library.changed", {"kind": "tracks", "ids": []})


def _write_wav(path: Path, seconds: float = 0.1) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(44100 * seconds)
    with wave.open(str(path), "w") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(44100)
        handle.writeframes(struct.pack("<" + "h" * frames, *([0] * frames)))
    return path


@pytest.fixture
def library(tmp_path: Path) -> Path:
    root = tmp_path / "library"
    _write_wav(root / "one.wav")
    _write_wav(root / "two.wav")
    return root


@pytest.fixture
def empty_data_dir(tmp_path: Path) -> Path:
    target = tmp_path / "data"
    (target / "state").mkdir(parents=True)
    return target


@pytest.fixture
def hub() -> Iterator[RecordingHub]:
    recorder = RecordingHub()
    set_hub(recorder)
    yield recorder
    set_hub(None)


@pytest.fixture
def live_client(
    empty_data_dir: Path, tmp_path: Path, hub: RecordingHub
) -> Iterator[TestClient]:
    register_progress_observer(SETUP_IMPORT_KIND, on_setup_import_progress)
    store = JobStore(
        tmp_path / "jobs.db", boot_id="boot-library-changed", owner_pid=os.getpid()
    )
    store.recover()
    runner = JobRunner(store, poll_s=0.05)

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        await runner.start()
        try:
            yield
        finally:
            await runner.stop()

    app = FastAPI(lifespan=_lifespan)
    app.include_router(setup_router, prefix="/api/v1")
    app.include_router(jobs_router, prefix="/api/v1")
    app.state.engine_cfg = EngineConfig(data_dir=empty_data_dir)
    app.state.jobs_store = store
    app.state.jobs_runner = runner
    app.state.build_identity = build_identity("payload")
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        store.close()


def _await_terminal(client: TestClient, job_id: str) -> dict:
    deadline = time.monotonic() + JOB_DEADLINE_S
    while time.monotonic() < deadline:
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["status"] in {"succeeded", "failed", "cancelled", "unknown"}:
            return job
        time.sleep(POLL_S)
    raise AssertionError(
        f"job {job_id} was still {job['status']} after {JOB_DEADLINE_S}s"
    )


@pytest.mark.requirement("SETUP-18")
def test_folder_import_success_publishes_library_changed_tracks(
    live_client: TestClient,
    library: Path,
    hub: RecordingHub,
) -> None:
    """[if] setup import succeeds [then] library.changed tracks, [else stop]."""
    assert live_client.get("/api/v1/setup/status").json()["library_empty"]

    job_id = live_client.post(
        "/api/v1/setup/import/folder",
        json={"folders": [str(library)]},
    ).json()["id"]
    finished = _await_terminal(live_client, job_id)
    assert finished["status"] == "succeeded", finished.get("error")
    assert TRACKS_INVALIDATION in hub.events


def test_failed_import_does_not_publish_tracks_invalidation(
    live_client: TestClient,
    tmp_path: Path,
    hub: RecordingHub,
) -> None:
    job_id = live_client.post(
        "/api/v1/setup/import",
        json={"source": str(tmp_path / "definitely-not-here.db")},
    ).json()["id"]
    finished = _await_terminal(live_client, job_id)
    assert finished["status"] == "failed"
    assert detect.CODE_REKORDBOX_NOT_FOUND in finished["error"]
    assert TRACKS_INVALIDATION not in hub.events
