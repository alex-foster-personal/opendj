"""The census and reaper remove orphaned test leftovers and never a real service.

[if] the reaper kills a service or spares an orphaned test server [then] fail, [else stop].

Real processes only for reap; classification is also checked on synthetic
process tables, which is the functional core the census feeds.

Regression lines:
  - if orphan_reaper kills a long-lived service carrying a non-test AF_SERVICE_ID then broken
  - if orphan_reaper leaves an orphaned test-harness or agent server alive then broken
  - if a process with its OWN non-test AF_SERVICE_ID is reaped despite agent,
    runner or checkout markers then broken
  - if an orphan carrying only a `.test.` marker (unrecognized command) is not reaped
    then broken
  - if a `systemd --user` adoptee is not orphaned, or a tmux child is then broken
  - if reap reports a target it never signalled as killed then broken
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.orphan_reaper import reap
from scripts.process_census import Proc, Row, classify
from tests.support.process_probes import GONE_WITHIN_S, alive, orphan, wait_for

pytestmark = pytest.mark.requirement("DEVOPS-17")

REPO_ROOT = Path(__file__).resolve().parents[2]
REAPER_MODULE = "scripts.orphan_reaper"  # run with -m from REPO_ROOT


# ---------------------------------------------------------------- reaper


def _census_row(pid: int) -> dict:
    out = subprocess.run(
        [sys.executable, "-m", REAPER_MODULE, "census", "--json"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    ).stdout
    rows = [r for r in json.loads(out)["rows"] if r["pid"] == pid]
    assert rows, f"pid {pid} is not in the census at all: the control cannot fail"
    return rows[0]


def test_reaper_kills_orphaned_test_servers_and_spares_a_real_service(
    cleanup_pids: list[int],
) -> None:
    clean = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/")}
    # The service's command matches the census scope on purpose ("uvicorn"),
    # so the ONLY thing protecting it is its non-test AF_SERVICE_ID.
    # ...and every agent/runner heuristic points the other way on purpose: an
    # agent marker, a runner marker and a checkout cwd. Its OWN non-test id must win.
    service = orphan(
        [sys.executable, "-c", "import time; time.sleep(600)", "uvicorn-control-service"],
        {
            **clean,
            "AF_SERVICE_ID": "com.opendj.control-service",
            "CLAUDECODE": "1",
            "GITHUB_RUN_ID": "1",
        },
        str(REPO_ROOT),
    )
    test_server = orphan(
        [sys.executable, "-c", "import time; time.sleep(600)", "uvicorn-control-test-server"],
        {**clean, "AF_SERVICE_ID": "com.opendj.test.control"},
        "/",
    )
    agent_server = orphan(
        [sys.executable, "-c", "import time; time.sleep(600)", "pytest-control-agent-server"],
        # A Claude session stamps its OWN profile id; that is agent provenance,
        # not a service to protect (the overshoot of the own-id rule).
        {**clean, "CLAUDECODE": "1", "AF_SERVICE_ID": "com.af.claude-profiles.account9"},
        "/",
    )
    # A command the census does not recognize: ONLY its `.test.` marker can
    # bring it into scope (a custom binary, a title-rewriting server).
    marker_only = orphan(
        [sys.executable, "-c", "import time; time.sleep(600)", "marker-only-control"],
        {**clean, "AF_SERVICE_ID": "com.opendj.test.markeronly"},
        "/",
    )
    static_server = orphan(
        [sys.executable, "-m", "http.server", "0", "--bind", "127.0.0.1"],
        {**clean, "CLAUDECODE": "1"},
        "/",
    )
    doomed = [test_server, agent_server, marker_only, static_server]
    cleanup_pids.extend([service, *doomed])
    wait_for(
        lambda: all(alive(p) for p in (service, *doomed)),
        10,
        "controls to start",
    )

    assert _census_row(service)["verdict"] == "service"
    for pid in doomed:
        assert _census_row(pid)["verdict"] == "reapable", pid

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            REAPER_MODULE,
            "reap",
            "--min-age-s",
            "0",
            *(f"--only-pid={p}" for p in (service, *doomed)),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    report = json.loads(result.stdout)
    assert result.returncode == 0, result.stderr
    assert sorted(k["pid"] for k in report["kills"] if k["outcome"] == "killed") == sorted(doomed)
    assert report["killed"] == len(doomed)
    assert alive(service), "the reaper killed a legitimately long-lived service"
    wait_for(
        lambda: not any(alive(p) for p in doomed),
        GONE_WITHIN_S,
        "reaped servers to exit",
    )


def _proc(pid: int, ppid: int, command: str, **extra: object) -> Proc:
    return Proc(pid, ppid, pid, 3600, "S", "dev", command, start=f"t{pid}", **extra)  # type: ignore[arg-type]


def test_a_subreaper_adoptee_is_orphaned_and_a_tmux_child_is_not() -> None:
    """Pure classification: a `systemd --user` subreaper that adopted a test
    server is ABOVE the tree, never its session host (and never its supervisor)."""
    test_env = {"AF_SERVICE_ID": "com.opendj.test.adopted"}
    procs = {
        1: _proc(1, 0, "/sbin/init"),
        10: _proc(10, 1, "/usr/lib/systemd/systemd --user", supervisor="job"),
        20: _proc(20, 10, "uvicorn apps.engine_core", env=dict(test_env)),
        30: _proc(30, 1, "tmux new-session -d"),
        40: _proc(40, 30, "uvicorn apps.engine_core", env=dict(test_env)),
    }
    rows = {r.pid: r for r in classify(procs)}
    assert (rows[20].tree, rows[20].verdict) == ("orphaned", "reapable"), rows[20]
    assert (rows[40].tree, rows[40].verdict) == ("session", "active"), rows[40]


def test_reap_reports_a_target_that_vanished_before_its_signal_as_not_killed() -> None:
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    procs = {gone.pid: _proc(gone.pid, 1, "pytest leftover", env={"CLAUDECODE": "1"})}
    rows = [
        Row(
            pid=gone.pid,
            ppid=1,
            pgid=gone.pid,
            root_pid=gone.pid,
            root_command="pytest leftover",
            age_s=3600,
            state="S",
            cwd="/",
            command="pytest leftover",
            env={"CLAUDECODE": "1"},
            env_markers=[],
            tree="orphaned",
            attribution="agent",
            verdict="reapable",
        )
    ]
    report = reap(procs, rows, min_age_s=0, dry_run=False)
    assert [k["outcome"] for k in report["kills"]] == ["gone-before-signal"]
    assert report["killed"] == 0
