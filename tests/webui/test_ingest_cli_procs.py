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


def test_run_cli_registers_only_while_the_step_runs(monkeypatch) -> None:
    seen: list[bool] = []
    real_register = ingest_cli_procs.register

    def _spy(proc: subprocess.Popen[str]) -> None:
        real_register(proc)
        seen.append(proc in ingest_cli_procs._procs)

    monkeypatch.setattr(ingest_cli_procs, "register", _spy)
    job = ingest_job._RefreshJob(started_at=time.time(), steps=[])
    ingest_job._run_cli(job, [sys.executable, "-c", "print('ok')"])

    assert seen == [True], "the step was not registered while it ran"
    assert ingest_cli_procs.stop_all() == 0, "a finished step stayed registered"


def test_the_lifespan_stops_running_clis(monkeypatch, tmp_path) -> None:
    calls: list[int] = []
    def _record() -> int:
        calls.append(1)
        return 0

    monkeypatch.setattr(ingest_cli_procs, "stop_all", _record)
    armed = app_mod.create_app(
        state_db_path=str(tmp_path / "state" / "state.db"), mount_frontend=False,
        port=18735, frontend_port=19735,
    )
    with TestClient(armed) as client:
        assert client.get("/api/v1/health").status_code == 200
        assert calls == [], "stop_all ran before shutdown"
    assert calls == [1], "the lifespan shutdown did not stop running CLIs"
