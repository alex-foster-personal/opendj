"""The census and reaper remove orphaned test leftovers and never a real service.

[if] the reaper kills a service or spares an orphaned test server [then] fail, [else stop].

Real processes only: every test here runs the real snapshot (``ps``/``/proc``)
and classification against live pids. The synthetic-table UNIT tests of the
same functional core live in ``test_process_census_unit.py`` and are not
DEVOPS-17 release evidence.

Regression lines:
  - if orphan_reaper kills a long-lived service carrying a non-test AF_SERVICE_ID then broken
  - if orphan_reaper leaves an orphaned test-harness or agent server alive then broken
  - if a process with its OWN non-test AF_SERVICE_ID is reaped despite agent,
    runner or checkout markers then broken
  - if an orphan carrying only a `.test.` marker (unrecognized command) is not reaped
    then broken
  - if a real double-forked orphan is not classified orphaned, or a live child
    of a running test is, then broken
  - if a real orphan whose own id merely mentions codex is not a service then broken
  - if a real process adopted by a Linux subreaper is not classified orphaned then broken
  - if a real retitled service (env unreadable on macOS) is reaped then broken
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from scripts.process_census import Row, classify, snapshot
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


RETITLE_CODE = (
    "import setproctitle, time; "
    "setproctitle.setproctitle('uvicorn apps.engine_core retitled-control'); time.sleep(600)"
)


def test_a_retitled_service_survives_reap(cleanup_pids: list[int]) -> None:
    """Real process: a daemonized service that rewrites its title, run from a
    checkout with an agent marker. On macOS its env is unreadable (unreadable-
    orphan); where it stays readable its own id makes it a service. Never reaped."""
    service = orphan(
        [sys.executable, "-c", RETITLE_CODE],
        {
            "PATH": os.environ["PATH"],
            "HOME": os.environ.get("HOME", "/"),
            "AF_SERVICE_ID": "com.opendj.control-retitled",
            "CLAUDECODE": "1",
        },
        str(REPO_ROOT),
    )
    cleanup_pids.append(service)
    wait_for(lambda: alive(service), 10, "retitled control to start")
    wait_for(lambda: "retitled-control" in _census_row(service)["command"], 10, "the title rewrite")
    verdict = _census_row(service)["verdict"]
    expected = (
        {"unreadable-orphan"} if sys.platform == "darwin" else {"unreadable-orphan", "service"}
    )
    assert verdict in expected, verdict
    result = subprocess.run(
        [sys.executable, "-m", REAPER_MODULE, "reap", "--min-age-s", "0", f"--only-pid={service}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["killed"] == 0
    assert alive(service), "the reaper killed a service whose own id it could not read"


def _classify_live(pids: list[int]) -> dict[int, Row]:
    """The real census, in process: snapshot this host, classify, pick ``pids``."""
    rows = {r.pid: r for r in classify(snapshot())}
    missing = [p for p in pids if p not in rows]
    assert not missing, f"pids {missing} are not in the census scope: the assertions cannot fail"
    return {p: rows[p] for p in pids}


SLEEP_600 = "import time; time.sleep(600)"


def test_the_census_orphans_a_real_double_forked_process_and_not_a_live_child(
    cleanup_pids: list[int],
) -> None:
    clean = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/")}
    orphaned_test_server = orphan(
        [sys.executable, "-c", SLEEP_600, "uvicorn-real-orphan"],
        {**clean, "AF_SERVICE_ID": "com.opendj.test.realorphan"},
        "/",
    )
    orphaned_service = orphan(
        [sys.executable, "-c", SLEEP_600, "uvicorn-real-service"],
        {**clean, "AF_SERVICE_ID": "com.af.codex-transcript-cleanup", "CLAUDECODE": "1"},
        "/",
    )
    # The control: same command shape and id, but a live child of this test.
    attached = subprocess.Popen(
        [sys.executable, "-c", SLEEP_600, "uvicorn-real-attached"],
        env={**clean, "AF_SERVICE_ID": "com.opendj.test.realattached"},
    )
    cleanup_pids.extend([orphaned_test_server, orphaned_service, attached.pid])
    wait_for(
        lambda: all(alive(p) for p in (orphaned_test_server, orphaned_service, attached.pid)),
        10,
        "real processes to start",
    )
    rows = _classify_live([orphaned_test_server, orphaned_service, attached.pid])
    assert (rows[orphaned_test_server].tree, rows[orphaned_test_server].verdict) == (
        "orphaned",
        "reapable",
    ), rows[orphaned_test_server]
    assert (rows[orphaned_service].tree, rows[orphaned_service].verdict) == (
        "orphaned",
        "service",
    ), rows[orphaned_service]
    assert rows[attached.pid].tree != "orphaned", rows[attached.pid]
    assert rows[attached.pid].verdict != "reapable", rows[attached.pid]


# A real Linux subreaper, titled like the `systemd --user` the census knows,
# that double-forks a test server so the kernel reparents it to the subreaper.
SUBREAPER_CODE = textwrap.dedent(
    """
    import ctypes, json, os, sys, time
    import setproctitle
    PR_SET_CHILD_SUBREAPER = 36
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        raise SystemExit(f"[ERROR] prctl subreaper failed: errno {ctypes.get_errno()}")
    setproctitle.setproctitle("/usr/lib/systemd/systemd --user subreaper-control")
    argv, env, out = json.loads(sys.argv[1])
    if os.fork() == 0:
        pid = os.fork()
        if pid == 0:
            os.execve(argv[0], argv, env)
        with open(out + ".tmp", "w") as fh:
            fh.write(str(pid))
        os.replace(out + ".tmp", out)
        os._exit(0)
    while True:
        try:
            os.waitpid(-1, 0)
        except ChildProcessError:
            time.sleep(1)
    """
)


def _linux_ppid(pid: int) -> int | None:
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return int(text[text.rfind(")") + 2 :].split()[1])


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="UNAVAILABLE: PR_SET_CHILD_SUBREAPER is Linux-only; macOS reparents every orphan "
    "to launchd, which the real double-fork test covers",
)
def test_the_census_orphans_a_real_subreaper_adoptee(
    tmp_path: Path, cleanup_pids: list[int]
) -> None:
    pid_file = tmp_path / "adoptee.pid"
    adoptee_argv = [sys.executable, "-c", SLEEP_600, "uvicorn-real-adoptee"]
    adoptee_env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", "/"),
        "AF_SERVICE_ID": "com.opendj.test.adoptee",
    }
    subreaper = subprocess.Popen(
        [
            sys.executable,
            "-c",
            SUBREAPER_CODE,
            json.dumps([adoptee_argv, adoptee_env, str(pid_file)]),
        ]
    )
    cleanup_pids.append(subreaper.pid)
    wait_for(pid_file.exists, 30, "the subreaper to fork its adoptee")
    adoptee = int(pid_file.read_text())
    cleanup_pids.append(adoptee)
    wait_for(
        lambda: _linux_ppid(adoptee) == subreaper.pid,
        10,
        f"the kernel to reparent {adoptee} to subreaper {subreaper.pid}",
    )
    row = _classify_live([adoptee])[adoptee]
    assert (row.tree, row.verdict, row.root_pid) == ("orphaned", "reapable", adoptee), row
