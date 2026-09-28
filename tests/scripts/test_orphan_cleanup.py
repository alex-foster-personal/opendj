"""Test-spawned servers never outlive their run; the reaper spares real services.

[if] a test server outlives its owner run [then] fail, [else stop].

Real processes only: every assertion here is about a live pid on this host.

Regression lines:
  - if a spawned server is alive after its fixture tears down then broken
  - if a spawned server is alive after a FAILING test's teardown then broken
  - if a spawned server is alive 5 s after pytest is SIGKILLed then broken
  - if a guarded Playwright webServer is alive 5 s after the runner is
    SIGTERMed or SIGKILLed then broken
  - if a spawned server's env lacks AF_SERVICE_ID=com.opendj.test.<name> then broken
  - if the guard starts a server for an owner pid that is not running then broken
  - if orphan_reaper kills a long-lived service carrying a non-test AF_SERVICE_ID then broken
  - if orphan_reaper leaves an orphaned test-harness or agent server alive then broken
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import textwrap
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from scripts.server_owner_guard import process_start_time
from tests.support.spawned_servers import SpawnedServer, spawn_test_server

pytestmark = pytest.mark.requirement("DEVOPS-17")

REPO_ROOT = Path(__file__).resolve().parents[2]
REAPER_MODULE = "scripts.orphan_reaper"  # run with -m from REPO_ROOT
FRONTEND = REPO_ROOT / "apps" / "webui" / "frontend"
PLAYWRIGHT_BIN = FRONTEND / "node_modules" / ".bin" / "playwright"
GONE_WITHIN_S = 5.0

# A server that proves it started (pidfile) and reports what it inherited.
SERVER_CODE = textwrap.dedent(
    """
    import json, os, sys, time
    out = sys.argv[1]
    tmp = out + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"pid": os.getpid(), "af_service_id": os.environ.get("AF_SERVICE_ID")}, fh)
    os.replace(tmp, out)
    time.sleep(600)
    """
)


# ---------------------------------------------------------------- helpers


def _alive(pid: int) -> bool:
    return process_start_time(pid) is not None


def _wait_for(predicate: Callable[[], bool], timeout_s: float, what: str) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout_s}s waiting for {what}")
        time.sleep(0.1)


def _read_server_info(info_path: Path, timeout_s: float = 30.0) -> dict:
    _wait_for(info_path.exists, timeout_s, f"server to write {info_path}")
    return json.loads(info_path.read_text())


def _server_argv(info_path: Path, title: str = "server") -> list[str]:
    return [sys.executable, "-c", SERVER_CODE, str(info_path), title]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _orphan(argv: list[str], env: dict[str, str], cwd: str) -> int:
    """Start argv detached and reparented to init; return its pid."""
    launcher = textwrap.dedent(
        """
        import json, os, sys
        argv, env, cwd = json.loads(sys.argv[1])
        r, w = os.pipe()
        if os.fork():
            os.close(w)
            sys.stdout.write(os.read(r, 32).decode())
            sys.exit(0)
        os.close(r)
        os.setsid()
        pid = os.fork()
        if pid:
            os.write(w, str(pid).encode())
            os._exit(0)
        devnull = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(devnull, fd)  # release the launcher's capture pipe
        os.chdir(cwd)
        os.execve(argv[0], argv, env)
        """
    )
    out = subprocess.run(
        [sys.executable, "-c", launcher, json.dumps([argv, env, cwd])],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return int(out.stdout)


@pytest.fixture
def cleanup_pids() -> Iterator[list[int]]:
    """Belt and braces: whatever a test leaves alive by FAILING is killed here."""
    pids: list[int] = []
    yield pids
    for pid in pids:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


def _inner_pytest(tmp_path: Path, body: str) -> subprocess.Popen[str]:
    test_file = tmp_path / "test_inner.py"
    test_file.write_text(textwrap.dedent(body))
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "tests.support.spawned_servers",
            "-p",
            "no:cacheprovider",
            "-c",
            str(tmp_path / "pytest.ini"),
            "--rootdir",
            str(tmp_path),
            str(test_file),
        ],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


INNER_TEMPLATE = """
import sys, textwrap, time
from pathlib import Path
SERVER_CODE = {server_code!r}
INFO = Path({info!r})

def test_spawns(spawned_server):
    spawned_server("inner", [sys.executable, "-c", SERVER_CODE, str(INFO)])
    deadline = time.monotonic() + 30
    while not INFO.exists():
        assert time.monotonic() < deadline, "inner server never started"
        time.sleep(0.05)
    {tail}

def test_previous_server_died_at_its_own_teardown():
    import json, os
    pid = json.loads(INFO.read_text())["pid"]
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    raise AssertionError(f"server {{pid}} outlived the fixture that spawned it")
"""


# ---------------------------------------------------------------- pytest side


@pytest.mark.parametrize(
    ("tail", "expect_summary"),
    [("assert True", "2 passed"), ("assert False, 'deliberate failure'", "1 failed, 1 passed")],
    ids=["passing-test", "failing-test"],
)
def test_server_is_gone_after_teardown(
    tmp_path: Path, cleanup_pids: list[int], tail: str, expect_summary: str
) -> None:
    """The SECOND inner test checks the first one's server, so this pins the
    FIXTURE finalizer; the session-end sweep alone would run too late."""
    info = tmp_path / "server.json"
    inner = _inner_pytest(
        tmp_path, INNER_TEMPLATE.format(server_code=SERVER_CODE, info=str(info), tail=tail)
    )
    out, _ = inner.communicate(timeout=120)
    server = _read_server_info(info, timeout_s=1)
    cleanup_pids.append(server["pid"])
    assert expect_summary in out, out
    assert not _alive(server["pid"]), f"server {server['pid']} outlived the inner run:\n{out}"


def test_server_is_gone_after_pytest_is_sigkilled(tmp_path: Path, cleanup_pids: list[int]) -> None:
    info = tmp_path / "server.json"
    inner = _inner_pytest(
        tmp_path,
        INNER_TEMPLATE.format(server_code=SERVER_CODE, info=str(info), tail="time.sleep(600)"),
    )
    server = _read_server_info(info)
    cleanup_pids.append(server["pid"])
    assert _alive(server["pid"]), "positive control: the server must be running before the kill"
    inner.kill()  # SIGKILL: no finalizer, no sessionfinish, no atexit
    inner.communicate(timeout=30)
    _wait_for(
        lambda: not _alive(server["pid"]),
        GONE_WITHIN_S,
        f"server {server['pid']} to die with its owner",
    )


def test_server_carries_its_testing_service_id(tmp_path: Path, cleanup_pids: list[int]) -> None:
    info = tmp_path / "server.json"
    server: SpawnedServer = spawn_test_server("idcheck", _server_argv(info))
    try:
        data = _read_server_info(info)
        cleanup_pids.append(data["pid"])
        assert data["af_service_id"] == "com.opendj.test.idcheck"
    finally:
        server.stop()
    assert not _alive(data["pid"])


def test_sigterm_to_the_guard_kills_the_server(tmp_path: Path, cleanup_pids: list[int]) -> None:
    info = tmp_path / "server.json"
    server = spawn_test_server("sigterm", _server_argv(info))
    data = _read_server_info(info)
    cleanup_pids.append(data["pid"])
    os.kill(server.pid, signal.SIGTERM)  # the guard only, not its group
    _wait_for(lambda: not _alive(data["pid"]), GONE_WITHIN_S, "server to die with its guard")
    server.stop()


def test_guard_refuses_an_owner_that_is_not_running(tmp_path: Path) -> None:
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    info = tmp_path / "server.json"
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "server_owner_guard.py"),
            "--owner-pid",
            str(dead.pid),
            "--name",
            "unowned",
            "--",
            *_server_argv(info),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0
    assert "is not running" in result.stderr
    time.sleep(0.5)
    assert not info.exists(), "the guard started a server with no live owner"


# ---------------------------------------------------------------- playwright side


PLAYWRIGHT_CONFIG = """
import {{ defineConfig }} from '@playwright/test';
import {{ guardedWebServerCommand }} from '../tests/e2e/support/guarded-web-server';
export default defineConfig({{
    testDir: '.',
    testMatch: 'hang.spec.ts',
    timeout: 300_000,
    webServer: [{{
        command: guardedWebServerCommand('orphan-test', {command!r}),
        url: 'http://127.0.0.1:{port}',
        reuseExistingServer: false,
        timeout: 60_000,
    }}],
}});
"""


def _port_holder(port: int) -> int | None:
    out = subprocess.run(
        ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()
    return int(out[0]) if out else None


@pytest.mark.skipif(
    not PLAYWRIGHT_BIN.exists(), reason="frontend node_modules not installed (pnpm install)"
)
@pytest.mark.skipif(shutil.which("lsof") is None, reason="needs lsof to find the port holder")
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGKILL], ids=["SIGTERM", "SIGKILL"])
def test_guarded_webserver_dies_when_the_playwright_runner_is_killed(
    cleanup_pids: list[int], sig: signal.Signals
) -> None:
    port = _free_port()
    work = FRONTEND / f".tmp-orphan-guard-{uuid.uuid4().hex[:8]}"
    work.mkdir()
    try:
        server_command = f"{sys.executable} -m http.server {port} --bind 127.0.0.1"
        (work / "guard.config.ts").write_text(
            PLAYWRIGHT_CONFIG.format(command=server_command, port=port)
        )
        (work / "hang.spec.ts").write_text(
            "import { test } from '@playwright/test';\n"
            "test('hang', async () => { await new Promise((r) => setTimeout(r, 290_000)); });\n"
        )
        runner = subprocess.Popen(
            [str(PLAYWRIGHT_BIN), "test", "-c", str(work / "guard.config.ts")],
            cwd=FRONTEND,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _wait_for(lambda: _port_holder(port) is not None, 90, f"webServer on {port}")
        holder = _port_holder(port)
        assert holder is not None
        cleanup_pids.append(holder)
        runner.send_signal(sig)
        runner.wait(timeout=30)
        _wait_for(
            lambda: not _alive(holder),
            GONE_WITHIN_S,
            f"webServer {holder} to die with the runner ({sig.name})",
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _playwright_configs() -> list[Path]:
    return [
        FRONTEND / "playwright.config.ts",
        *sorted((FRONTEND / "tests" / "e2e").glob("playwright.*.config.ts")),
    ]


def test_every_playwright_webserver_command_is_guarded() -> None:
    """The class, not an instance: a new config with a bare webServer command
    would leak exactly like the 43 this change wrapped."""
    configs = _playwright_configs()
    assert len(configs) > 20, (
        f"found only {len(configs)} configs: the glob is wrong, not the tree clean"
    )
    commands = 0
    unguarded: list[str] = []
    for config in configs:
        for match in re.finditer(r"\bcommand:\s*(\S{0,40})", config.read_text()):
            commands += 1
            if not match.group(1).startswith("guardedWebServerCommand("):
                unguarded.append(f"{config.name}: command: {match.group(1)}")
    assert commands >= 40, f"only {commands} webServer commands found: the pattern stopped matching"
    assert not unguarded, "unguarded webServer commands:\n" + "\n".join(unguarded)


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
    service = _orphan(
        [sys.executable, "-c", "import time; time.sleep(600)", "uvicorn-control-service"],
        {**clean, "AF_SERVICE_ID": "com.opendj.control-service"},
        "/",
    )
    test_server = _orphan(
        [sys.executable, "-c", "import time; time.sleep(600)", "uvicorn-control-test-server"],
        {**clean, "AF_SERVICE_ID": "com.opendj.test.control"},
        "/",
    )
    agent_server = _orphan(
        [sys.executable, "-c", "import time; time.sleep(600)", "pytest-control-agent-server"],
        {**clean, "CLAUDECODE": "1"},
        "/",
    )
    cleanup_pids.extend([service, test_server, agent_server])
    _wait_for(
        lambda: all(_alive(p) for p in (service, test_server, agent_server)),
        10,
        "controls to start",
    )

    assert _census_row(service)["verdict"] == "service"
    assert _census_row(test_server)["verdict"] == "reapable"
    assert _census_row(agent_server)["verdict"] == "reapable"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            REAPER_MODULE,
            "reap",
            "--min-age-s",
            "0",
            *(f"--only-pid={p}" for p in (service, test_server, agent_server)),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    report = json.loads(result.stdout)
    assert result.returncode == 0, result.stderr
    assert sorted(k["pid"] for k in report["kills"] if k["outcome"] == "killed") == sorted(
        [test_server, agent_server]
    )
    assert report["killed"] == 2
    assert _alive(service), "the reaper killed a legitimately long-lived service"
    _wait_for(
        lambda: not _alive(test_server) and not _alive(agent_server),
        GONE_WITHIN_S,
        "reaped servers to exit",
    )
