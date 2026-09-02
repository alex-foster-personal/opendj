"""The worker subprocess, and the job that spawns it.

The runner kills any worker that writes a line which is not the progress
contract, so "stdout carries nothing else" is not a style preference -- it
is the difference between an import that finishes and one that dies with a
protocol error. The ingest CLI prints a full summary, which makes this a
live hazard rather than a theoretical one.

Single-line intent:
  - if anything but progress JSON reaches stdout then the runner kills the
    import mid-write
  - if the ingest summary is swallowed instead of routed to stderr then a
    failed import has no diagnostics
  - if a refusal exits 0 then a job that imported nothing reports success
  - if the argv builder accepts a relative data dir then the worker imports
    into whatever directory the engine happened to be started from
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.engine_core.config import EngineConfig
from apps.engine_core.jobs.api import router as jobs_router
from apps.engine_core.jobs.runner import JobRunner
from apps.engine_core.jobs.store import JobStore
from apps.engine_core.setup import detect, record
from apps.engine_core.setup.api import router as setup_router
from apps.engine_core.setup.jobs import (
    SETUP_IMPORT_KIND,
    SetupPayloadError,
    build_argv,
)
from tests.engine_core.conftest import build_identity

REPO_ROOT: Path = Path(__file__).resolve().parents[2]

#: A cold worker imports pyrekordbox and SQLAlchemy before it does any work,
#: so the budget is generous. It is a deadline, not an expected duration.
JOB_DEADLINE_S: float = 120.0
POLL_S: float = 0.2


@pytest.fixture
def data_dir(tmp_path: Path, rb_plain_db_path: Path) -> Path:
    """``rb_plain_db_path`` (root conftest) resolves, fails closed on a
    missing fixture host, and checksum-verifies -- routing through it here
    (rather than a hard-coded repo path) keeps this test working once
    ``tests/fixtures/rekordbox/`` leaves the repo (PR #718)."""
    target = tmp_path / "data"
    (target / "state").mkdir(parents=True)
    shutil.copy2(rb_plain_db_path, target / detect.PLAIN_COPY_NAME)
    return target


def _run_worker(data_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "apps.engine_core.setup.worker",
            "--data-dir",
            str(data_dir),
            *args,
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )


# ----- the progress protocol ---------------------------------------------
def test_worker_stdout_is_only_the_progress_protocol(data_dir: Path) -> None:
    result = _run_worker(data_dir)
    assert result.returncode == 0, result.stderr

    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines, "the worker reported no progress at all"
    for line in lines:
        parsed = json.loads(line)  # raises on the exact thing that kills a job
        assert isinstance(parsed, dict)
        assert isinstance(parsed["progress"], (int, float))
        assert not isinstance(parsed["progress"], bool)
        assert isinstance(parsed["message"], str)


def test_the_ingest_summary_goes_to_stderr_not_stdout(data_dir: Path) -> None:
    """Redirected, never suppressed: it is the job's diagnostics."""
    result = _run_worker(data_dir)
    assert "Rekordbox ingest" in result.stderr
    assert "Rekordbox ingest" not in result.stdout


def test_progress_ends_at_one_and_never_goes_backwards(
    data_dir: Path,
) -> None:
    result = _run_worker(data_dir)
    progresses = [
        json.loads(line)["progress"]
        for line in result.stdout.splitlines()
        if line.strip()
    ]
    assert progresses == sorted(progresses)
    assert progresses[-1] == pytest.approx(1.0)


# ----- refusals -----------------------------------------------------------
def test_a_refusal_exits_nonzero_and_names_its_code(tmp_path: Path) -> None:
    empty = tmp_path / "empty-data"
    empty.mkdir()
    result = _run_worker(empty, "--source", str(tmp_path / "nothing.db"))
    assert result.returncode != 0
    assert detect.CODE_REKORDBOX_NOT_FOUND in result.stderr
    assert result.stdout.count("\n") >= 1  # the starting line, and no more


def test_a_relative_data_dir_is_refused(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "apps.engine_core.setup.worker",
            "--data-dir",
            "relative/data",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "must be absolute" in result.stderr


# ----- argv ---------------------------------------------------------------
def test_build_argv_names_the_worker_module() -> None:
    argv = build_argv({"data_dir": "/tmp/library"})
    assert argv[:4] == [
        sys.executable,
        "-m",
        "apps.engine_core.setup.worker",
        "--data-dir",
    ]
    assert argv[4] == "/tmp/library"


def test_build_argv_carries_every_option() -> None:
    argv = build_argv(
        {
            "data_dir": "/tmp/library",
            "source": "/tmp/master.plain.db",
            "limit": 7,
            "refresh_decrypt": True,
        }
    )
    assert "--source" in argv and "/tmp/master.plain.db" in argv
    assert "--limit" in argv and "7" in argv
    assert "--refresh-decrypt" in argv


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data_dir": ""},
        {"data_dir": "relative/data"},
        {"data_dir": "/tmp/library", "source": "  "},
        {"data_dir": "/tmp/library", "limit": 0},
        {"data_dir": "/tmp/library", "limit": True},
        {"data_dir": "/tmp/library", "refresh_decrypt": "yes"},
    ],
)
def test_build_argv_refuses_a_payload_it_cannot_trust(
    payload: dict[str, object],
) -> None:
    with pytest.raises(SetupPayloadError):
        build_argv(payload)


# ----- end to end through the real runner ---------------------------------
@pytest.fixture
def live_client(data_dir: Path, tmp_path: Path) -> Iterator[TestClient]:
    """Setup router + jobs router + a RUNNING supervisor, one data dir."""
    store = JobStore(
        tmp_path / "jobs.db", boot_id="boot-e2e", owner_pid=os.getpid()
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
    app.state.engine_cfg = EngineConfig(data_dir=data_dir)
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


def test_the_import_job_really_populates_the_library(
    live_client: TestClient, data_dir: Path
) -> None:
    """The whole deliverable in one assertion: empty library in, library out.

    Runs the real router, the real supervisor, a real forked worker and the
    real ingest against the committed fixture. Nothing is stubbed.
    """
    assert live_client.get("/api/v1/setup/status").json()["library_empty"]

    job_id = live_client.post("/api/v1/setup/import", json={}).json()["id"]
    finished = _await_terminal(live_client, job_id)
    assert finished["status"] == "succeeded", finished["error"]
    assert finished["progress"] == pytest.approx(1.0)

    status = live_client.get("/api/v1/setup/status").json()
    assert status["library_empty"] is False
    assert status["tracks"] > 0
    assert status["should_show_wizard"] is False
    assert status["last_import"]["tracks"] == status["tracks"]
    assert record.read(data_dir).last_import is not None


def test_a_failed_import_job_reports_its_code_on_the_row(
    live_client: TestClient, tmp_path: Path
) -> None:
    """The refusal has to survive the trip out of the subprocess."""
    job_id = live_client.post(
        "/api/v1/setup/import",
        json={"source": str(tmp_path / "definitely-not-here.db")},
    ).json()["id"]
    finished = _await_terminal(live_client, job_id)
    assert finished["status"] == "failed"
    assert detect.CODE_REKORDBOX_NOT_FOUND in finished["error"]


def test_the_kind_the_endpoint_enqueues_is_the_kind_that_is_registered(
    live_client: TestClient,
) -> None:
    kinds = live_client.get("/api/v1/jobs/kinds").json()["kinds"]
    assert SETUP_IMPORT_KIND in kinds
