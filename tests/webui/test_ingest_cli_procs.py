"""Shutdown stops the pipeline CLIs a refresh job has running (quit hang, Fri 2 Oct 2026).

[if] the engine shuts down mid-step [then] the CLI and its pool workers keep running after the app is gone, [else stop].

On demon-llama an engine stopped by a plain SIGTERM left the analysis CLI's
pool workers running after the app was gone, and they blocked the DMG
installer. These cases drive real processes: a CLI stand-in that starts a
grandchild, the way ``apps.analysis.run`` starts its pool.

Regression lines:
  - if stop_all leaves the CLI's descendants running then the pool workers
    outlive the engine -> broken
  - if the stopped CLI reads as exit 0 then a killed step reports success -> broken
  - if stop_all touches a process nobody registered then it kills work it does
    not own -> broken (the overshoot)
  - if stop_all waits per CLI or per wait instead of once overall then two
    CLIs that ignore SIGTERM outlast the shell's grace -> broken
  - if a finished step stays registered then shutdown signals a dead pid -> broken
  - if the lifespan never calls stop_all then none of this runs -> broken
"""
from __future__ import annotations

import contextlib
import subprocess
import sys
import threading
import time

import psutil
import pytest
from fastapi.testclient import TestClient

from apps.webui.server import app as app_mod
from apps.webui.server.routes import ingest_cli_procs, ingest_job

pytestmark = [pytest.mark.requirement("INSTALL-33"), pytest.mark.skipif(
    sys.platform == "win32", reason="the CLI stand-in prints a POSIX grandchild pid"
)]

# A CLI stand-in: starts a long-lived grandchild, prints its pid, then waits.
_CLI_WITH_POOL = (
    "import subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "print(child.pid, flush=True)\n"
    "time.sleep(60)\n"
)


# The same CLI, but it and its grandchild both ignore SIGTERM.
_STUBBORN_CLI_WITH_POOL = (
    "import signal, subprocess, sys, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "child = subprocess.Popen([sys.executable, '-c', "
    "'import signal, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "print(1, flush=True); time.sleep(60)'], stdout=subprocess.PIPE)\n"
    "child.stdout.readline()\n"
    "print(child.pid, flush=True)\n"
    "time.sleep(60)\n"
)


def _start_cli(source: str = _CLI_WITH_POOL) -> tuple[subprocess.Popen[str], int]:
    proc = subprocess.Popen(
        [sys.executable, "-c", source], stdout=subprocess.PIPE, text=True
    )
    assert proc.stdout is not None
    grandchild = int(proc.stdout.readline())
    return proc, grandchild


def _gone(pid: int, within_s: float = 10.0) -> bool:
    deadline = time.monotonic() + within_s
    while time.monotonic() < deadline:
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return True
        except psutil.NoSuchProcess:
            return True
        time.sleep(0.05)
    return False


def _kill(proc: subprocess.Popen[str], pid: int) -> None:
    for target in (pid, proc.pid):
        with contextlib.suppress(psutil.NoSuchProcess):
            psutil.Process(target).kill()
    proc.wait()


def test_stop_all_stops_the_cli_and_its_descendants() -> None:
    proc, grandchild = _start_cli()
    try:
        ingest_cli_procs.register(proc)
        assert psutil.pid_exists(grandchild), "control: the grandchild must be running"

        assert ingest_cli_procs.stop_all() == 1

        assert proc.poll() is not None, "the CLI is still running"
        assert proc.returncode != 0, f"a stopped CLI read as exit {proc.returncode}"
        assert _gone(grandchild), "the CLI's grandchild outlived the shutdown"
    finally:
        _kill(proc, grandchild)


def test_stop_all_is_bounded_when_every_cli_ignores_sigterm() -> None:
    started = [_start_cli(_STUBBORN_CLI_WITH_POOL) for _ in range(2)]
    try:
        for proc, _ in started:
            ingest_cli_procs.register(proc)

        began = time.monotonic()
        assert ingest_cli_procs.stop_all() == 2
        took = time.monotonic() - began

        # Waiting tree by tree would take at least 2 * STOP_GRACE_S here.
        assert took <= ingest_cli_procs.STOP_ALL_MAX_S + 0.5, f"stop_all took {took:.1f}s"
        for proc, grandchild in started:
            assert proc.poll() is not None, "a stubborn CLI is still running"
            assert proc.returncode != 0
            assert _gone(grandchild), "a stubborn grandchild outlived the shutdown"
    finally:
        for proc, grandchild in started:
            _kill(proc, grandchild)


def test_stop_all_leaves_unregistered_processes_alone() -> None:
    proc, grandchild = _start_cli()
    try:
        ingest_cli_procs.register(proc)
        ingest_cli_procs.unregister(proc)

        assert ingest_cli_procs.stop_all() == 0

        assert proc.poll() is None, "stop_all killed a CLI it no longer owned"
        assert psutil.pid_exists(grandchild)
    finally:
        _kill(proc, grandchild)


def _run_cli_in_thread(source: str) -> tuple[threading.Thread, ingest_job._RefreshJob, list[BaseException]]:
    job = ingest_job._RefreshJob(started_at=time.time(), steps=[])
    raised: list[BaseException] = []

    def _step() -> None:
        try:
            ingest_job._run_cli(job, [sys.executable, "-c", source])
        except BaseException as exc:  # noqa: BLE001 - handed back to the test
            raised.append(exc)

    thread = threading.Thread(target=_step, daemon=True)
    thread.start()
    return thread, job, raised


def _grandchild_from_log(job: ingest_job._RefreshJob, within_s: float = 30.0) -> int:
    deadline = time.monotonic() + within_s
    while time.monotonic() < deadline:
        for line in list(job.log):
            last = line.rsplit(" ", 1)[-1]
            if last.isdigit():
                return int(last)
        time.sleep(0.05)
    raise AssertionError(f"the CLI never printed its grandchild pid: {job.log!r}")


def test_a_finished_step_leaves_nothing_registered() -> None:
    thread, _job, raised = _run_cli_in_thread("print('ok')")
    thread.join(timeout=30)
    assert not thread.is_alive() and raised == []
    assert ingest_cli_procs.stop_all() == 0, "a finished step stayed registered"


def test_the_lifespan_stops_a_running_step_and_its_pool(tmp_path) -> None:
    armed = app_mod.create_app(
        state_db_path=str(tmp_path / "state" / "state.db"), mount_frontend=False,
        port=18735, frontend_port=19735,
    )
    with TestClient(armed) as client:
        assert client.get("/api/v1/health").status_code == 200
        thread, job, raised = _run_cli_in_thread(_CLI_WITH_POOL)
        grandchild = _grandchild_from_log(job)
        assert thread.is_alive(), "the step ended before shutdown"
    try:
        thread.join(timeout=30)
        assert _gone(grandchild), "the lifespan shutdown left the pool worker running"
        assert not thread.is_alive(), "the lifespan shutdown left the step running"
        assert raised, "a step stopped by shutdown read as success"
        assert ingest_cli_procs.stop_all() == 0, "the stopped step stayed registered"
    finally:
        with contextlib.suppress(psutil.NoSuchProcess):
            psutil.Process(grandchild).kill()
