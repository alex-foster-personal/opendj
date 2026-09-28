"""Test-spawned servers never outlive their run (guard, pytest fixture, Playwright).

[if] a test server outlives its owner run [then] fail, [else stop].

Real processes only: every assertion here is about a live pid on this host.
The census and reaper are covered in ``test_orphan_reaper.py``; the guard's
owner-death deadline and spawn window in ``test_server_owner_guard.py``.

Regression lines:
  - if a spawned server is alive after its fixture tears down then broken
  - if a spawned server is alive after a FAILING test's teardown then broken
  - if a spawned server is alive 5 s after pytest is SIGKILLed then broken
  - if a guarded Playwright webServer is alive 5 s after the runner is
    SIGTERMed or SIGKILLed then broken
  - if a spawned server's env lacks AF_SERVICE_ID=com.opendj.test.<name> then broken
  - if the guard starts a server for an owner pid that is not running then broken
  - if a root-conftest pytest plugin needs more than stdlib + pytest to import then broken
  - if a server the guarded child backgrounded outlives the child, or the guard's
    status differs from the child's then broken
  - if the guard starts while not leading its process group then broken
  - if cleanup_pids SIGKILLs a pid whose start time changed since it was added then broken
"""

from __future__ import annotations

import ast
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import textwrap
import time
import uuid
from pathlib import Path

import pytest

from tests.support.process_probes import (
    GONE_WITHIN_S,
    SERVER_CODE,
    alive,
    server_argv,
    wait_for,
)
from tests.support.spawned_servers import PidsToKill, SpawnedServer, spawn_test_server

pytestmark = pytest.mark.requirement("DEVOPS-17")

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND = REPO_ROOT / "apps" / "webui" / "frontend"
PLAYWRIGHT_BIN = FRONTEND / "node_modules" / ".bin" / "playwright"

# ---------------------------------------------------------------- helpers


def _read_server_info(info_path: Path, timeout_s: float = 30.0) -> dict:
    wait_for(info_path.exists, timeout_s, f"server to write {info_path}")
    return json.loads(info_path.read_text())


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


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
    assert not alive(server["pid"]), f"server {server['pid']} outlived the inner run:\n{out}"


def test_server_is_gone_after_pytest_is_sigkilled(tmp_path: Path, cleanup_pids: list[int]) -> None:
    info = tmp_path / "server.json"
    inner = _inner_pytest(
        tmp_path,
        INNER_TEMPLATE.format(server_code=SERVER_CODE, info=str(info), tail="time.sleep(600)"),
    )
    server = _read_server_info(info)
    cleanup_pids.append(server["pid"])
    assert alive(server["pid"]), "positive control: the server must be running before the kill"
    inner.kill()  # SIGKILL: no finalizer, no sessionfinish, no atexit
    inner.communicate(timeout=30)
    wait_for(
        lambda: not alive(server["pid"]),
        GONE_WITHIN_S,
        f"server {server['pid']} to die with its owner",
    )


def test_server_carries_its_testing_service_id(tmp_path: Path, cleanup_pids: list[int]) -> None:
    info = tmp_path / "server.json"
    server: SpawnedServer = spawn_test_server("idcheck", server_argv(info))
    try:
        data = _read_server_info(info)
        cleanup_pids.append(data["pid"])
        assert data["af_service_id"] == "com.opendj.test.idcheck"
    finally:
        server.stop()
    assert not alive(data["pid"])


def test_sigterm_to_the_guard_kills_the_server(tmp_path: Path, cleanup_pids: list[int]) -> None:
    info = tmp_path / "server.json"
    server = spawn_test_server("sigterm", server_argv(info))
    data = _read_server_info(info)
    cleanup_pids.append(data["pid"])
    os.kill(server.pid, signal.SIGTERM)  # the guard only, not its group
    wait_for(lambda: not alive(data["pid"]), GONE_WITHIN_S, "server to die with its guard")
    server.stop()


def test_cleanup_pids_never_kills_a_pid_whose_start_time_changed() -> None:
    """A recycled pid carries a different start time than the one recorded."""
    stale = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    current = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    try:
        pids = PidsToKill()
        pids.extend([stale.pid, current.pid])
        pids.identities[stale.pid] = "a start time from before the pid was recycled"
        pids.kill_all()
        assert current.wait(timeout=10) == -signal.SIGKILL, "control: the pinned pid must die"
        time.sleep(0.2)
        assert stale.poll() is None, "cleanup_pids killed a pid whose start time changed"
    finally:
        for proc in (stale, current):
            proc.kill()
            proc.wait(timeout=10)


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
            *server_argv(info),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        start_new_session=True,  # so the refusal is about the owner, not group leadership
    )
    assert result.returncode != 0
    assert "is not running" in result.stderr
    time.sleep(0.5)
    assert not info.exists(), "the guard started a server with no live owner"


def _run_guard(argv: list[str], *, new_session: bool) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "server_owner_guard.py"),
            "--owner-pid",
            str(os.getpid()),
            "--name",
            "leftover",
            "--",
            *argv,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        start_new_session=new_session,
    )


def test_a_server_the_child_backgrounded_dies_when_the_child_exits(
    tmp_path: Path, cleanup_pids: list[int]
) -> None:
    pid_file = tmp_path / "background.pid"
    sleeper = f"{shlex.quote(sys.executable)} -c 'import time; time.sleep(600)'"
    result = _run_guard(
        ["/bin/sh", "-c", f"{sleeper} & echo $! > {shlex.quote(str(pid_file))}; exit 3"],
        new_session=True,
    )
    background = int(pid_file.read_text())
    cleanup_pids.append(background)
    assert result.returncode == 3, f"the child's status was not preserved: {result.stderr}"
    wait_for(
        lambda: not alive(background),
        GONE_WITHIN_S,
        f"backgrounded server {background} to die with its child",
    )


def test_the_guard_refuses_to_start_unless_it_leads_its_group(tmp_path: Path) -> None:
    """Without leadership a group signal would hit the CALLER (here, pytest)."""
    info = tmp_path / "server.json"
    result = _run_guard(server_argv(info), new_session=False)
    assert result.returncode != 0
    assert "must lead its process group" in result.stderr
    time.sleep(0.5)
    assert not info.exists(), "the guard started a server it could not contain"


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
        wait_for(lambda: _port_holder(port) is not None, 90, f"webServer on {port}")
        holder = _port_holder(port)
        assert holder is not None
        cleanup_pids.append(holder)
        runner.send_signal(sig)
        runner.wait(timeout=30)
        wait_for(
            lambda: not alive(holder),
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


# A pytest-only toolchain (ci.yml's quality job runs `uv run --isolated
# --no-project --with pytest`) has the stdlib, pytest and whatever pytest itself
# loads, plus this repo's own packages from the checkout. Nothing else.
FIRST_PARTY_PACKAGES = ("apps", "scripts", "tests")
_BARE_IMPORT_PROBE = """
import importlib, importlib.abc, sys
import pytest  # noqa: F401  (loads pytest's own dependencies BEFORE the gate)
allowed = set(sys.stdlib_module_names) | {m.split(".")[0] for m in sys.modules}
allowed |= set(sys.argv[1].split(","))
class RefuseThirdParty(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] not in allowed:
            raise ImportError(f"not in a pytest-only toolchain: {name}")
sys.meta_path.insert(0, RefuseThirdParty())
for module in sys.argv[2:]:
    importlib.import_module(module)
print("imported", len(sys.argv) - 2)
"""


def _import_with_pytest_only(modules: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", _BARE_IMPORT_PROBE, ",".join(FIRST_PARTY_PACKAGES), *modules],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _root_conftest_plugins() -> list[str]:
    tree = ast.parse((REPO_ROOT / "conftest.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "pytest_plugins" for t in node.targets
        ):
            return [ast.literal_eval(element) for element in node.value.elts]  # type: ignore[attr-defined]
    raise AssertionError("root conftest.py has no pytest_plugins list")


def test_root_conftest_plugins_import_with_pytest_only() -> None:
    """The class: the root conftest loads these in EVERY pytest run, including
    the quality job's pytest-only toolchain, where a psutil import killed
    collection (PR #4270, first CI run)."""
    plugins = _root_conftest_plugins()
    assert "tests.support.spawned_servers" in plugins, plugins
    blocked = _import_with_pytest_only(["psutil"])
    assert blocked.returncode != 0 and "not in a pytest-only toolchain: psutil" in blocked.stderr, (
        "the probe did not refuse psutil, so it cannot catch the defect it exists for"
    )
    result = _import_with_pytest_only(plugins)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip() == f"imported {len(plugins)}", result.stdout
