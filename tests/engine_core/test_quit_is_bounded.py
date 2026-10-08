"""A user quit ends the engine in seconds, with nothing left behind (W43, INSTALL-33).

[if] a long poll is open at SIGTERM [then] the quit stalls and the poll 500s, [else stop].

Seen on silver, Wed 7 Oct 2026, quitting the installed app built from main
2411a8f2: the shell logged ``engine pgid 46209 exited 13341ms after SIGTERM``
and then ``SIGKILLed processes left behind by the exited engine``, and a
``GET /api/v1/commands/next?wait_ms=20000`` long poll answered 500. These
cases boot the real, unmodified ``serve`` entry point against a real
``/performance`` long poll and send it a real SIGTERM.

Regression lines:
  - if a held long poll is not released when shutdown starts then the quit
    costs the whole graceful window and the poll is cancelled as a 500 ->
    broken
  - if releasing it also ends a long poll on a RUNNING engine then the
    AGENT-19 hold is gone (the overshoot control) -> broken
  - if the quit leaves a descendant process behind then the shell's group
    SIGKILL cannot reach a job worker that leads its own session -> broken
  - if the watchdog does not end a wedged shutdown then a quit has no bound
    at all -> broken
  - if the watchdog starts its reap AT the deadline then a child that ignores
    SIGTERM pushes the quit past it -> broken
  - if nested teardown groups each get their own stop window then their waits
    add up again, and a late group is never even told to stop -> broken
  - if the real engine's lifespans stop sharing one window then its own log
    shows two windows on a quit -> broken
  - if the watchdog's reap looks for children only once, or stops looking at
    the first empty look, then a worker forked while it runs outlives the
    engine -> broken
  - if an agent order is still waiting for a page at SIGTERM and does not
    answer 503 engine_quitting then it hangs until uvicorn cancels it -> broken
  - if the watchdog is armed on the event loop rather than in the signal
    handler then a loop blocked by synchronous work is never bounded -> broken
  - if the watchdog waits on a logging handler then a thread stuck mid-emit
    wedges the watchdog itself -> broken
  - if a subprocess can start after the quit's SIGTERM, or a child forked
    another way is still alive when the engine exits, then the shell SIGKILLs
    it as left behind (alpha.1, quitting mid-analysis) -> broken
"""
from __future__ import annotations

import http.client
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import psutil
import pytest
from starlette.datastructures import State

from apps.shared.shutdown_bound import (
    QUIT_DEADLINE_S,
    REAP_BUDGET_S,
    STOP_STEPS_DEADLINE_S,
    begin_stop_window,
    run_stop_steps,
)

pytestmark = [
    pytest.mark.requirement("INSTALL-33"),
    pytest.mark.requirement("INSTALL-36"),
]
#: Only the cases that deliver a real SIGTERM to a process are POSIX-only;
#: the stop-window cases use threads and monotonic time and run everywhere.
posix_sigterm = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX SIGTERM delivery to the engine process"
)

REPO = Path(__file__).resolve().parents[2]
#: The user-visible target from the W43 order: a quit inside 3 s.
QUIT_WITHIN_S: float = 3.0
#: The long poll the installed app's page holds open (AGENT-19).
LONG_POLL_MS: int = 20_000


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _boot(tmp_path: Path, entry: list[str] | None = None) -> tuple[subprocess.Popen[bytes], int]:
    """The real ``serve``; ``entry`` replaces ``-m apps.engine_core`` to wrap it."""
    port = _free_port()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    log = (tmp_path / "engine.log").open("w")
    proc = subprocess.Popen(
        [
            sys.executable, *(entry or ["-m", "apps.engine_core"]), "serve",
            "--data-dir", str(data_dir), "--port", str(port),
        ],
        cwd=REPO,
        # Launched the way the desktop shell launches it (engine.rs), whose
        # warning-log pair also turns on the INFO log the shutdown lines use.
        env={
            **os.environ,
            "MDT_LIBRARY_MODE": "local",
            "OPENDJ_ENGINE_WARN_LOG": str(tmp_path / "engine-warn.log"),
            "OPENDJ_ENGINE_LOG_BOOT_ID": f"shell-{os.getpid()}-{int(time.time() * 1000)}",
        },
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        assert proc.poll() is None, (tmp_path / "engine.log").read_text()[-2000:]
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/health", timeout=1)
        except OSError:
            time.sleep(0.2)
        else:
            return proc, port
    proc.kill()
    proc.wait()
    raise AssertionError("the engine never answered /api/v1/health")


def _open_page(port: int) -> None:
    """Publish a UI mirror, which is what makes the page's long poll legal."""
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/v1/state/ui-mirror",
        data=b'{"client_open": true}',
        method="PUT",
        headers={"Content-Type": "application/json"},
    )
    assert urllib.request.urlopen(request, timeout=5).status == 202


def _hold_long_poll(port: int) -> http.client.HTTPConnection:
    """Start the page's long poll and prove it is still held when we return."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    conn.request("GET", f"/api/v1/commands/next?wait_ms={LONG_POLL_MS}")
    time.sleep(1.0)
    conn.sock.setblocking(False)
    try:
        with pytest.raises(BlockingIOError):
            conn.sock.recv(1)  # no response yet: the poll is being held
    finally:
        conn.sock.setblocking(True)
    return conn


def _quit(proc: subprocess.Popen[bytes]) -> tuple[float | None, list[int]]:
    """SIGTERM the engine's group as the shell does; report the time and leftovers."""
    descendants = psutil.Process(proc.pid).children(recursive=True)
    started = time.monotonic()
    os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=QUIT_WITHIN_S + 25)
    except subprocess.TimeoutExpired:
        return None, [child.pid for child in descendants if child.is_running()]
    took = time.monotonic() - started
    time.sleep(0.2)
    return took, [child.pid for child in descendants if child.is_running()]


def _stop(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        proc.kill()
        proc.wait()


@posix_sigterm
def test_a_quit_with_a_page_long_poll_open_is_fast_and_clean(tmp_path: Path) -> None:
    proc, port = _boot(tmp_path)
    try:
        _open_page(port)
        conn = _hold_long_poll(port)
        took, left_behind = _quit(proc)
        assert took is not None, "the engine was still running 28s after SIGTERM"
        assert took < QUIT_WITHIN_S, f"quit took {took:.1f}s, over the {QUIT_WITHIN_S}s target"
        assert not left_behind, f"processes left behind by the exited engine: {left_behind}"
        # The held poll ends like a hold that ran out, not as a cancelled task.
        answer = conn.getresponse()
        assert answer.status == 200, f"the held long poll answered {answer.status}"
        answer.read()
        conn.close()
    finally:
        _stop(proc)


def _windows(log_text: str) -> dict[str, str]:
    """``{group label: "<shared|own> window <deadline>"}`` from the shutdown log."""
    found = re.findall(r"\[shutdown\] (\w+ lifespan): .*\[(\w+ window [\d.]+)\]", log_text)
    return dict(found)


@posix_sigterm
def test_the_real_lifespans_share_one_stop_window_on_a_quit(tmp_path: Path) -> None:
    """The production nesting, not a model of it: engine and legacy stops, one window."""
    proc, _port = _boot(tmp_path)
    try:
        took, left_behind = _quit(proc)
        assert took is not None and took < QUIT_WITHIN_S, took
        assert not left_behind, left_behind
    finally:
        _stop(proc)
    windows = _windows((tmp_path / "engine.log").read_text())
    # Presence first: both groups ran and logged, so an empty match cannot pass.
    assert set(windows) == {"engine lifespan", "webui lifespan"}, windows
    assert windows["engine lifespan"].startswith("shared window "), windows
    assert windows["webui lifespan"] == windows["engine lifespan"], (
        f"the legacy lifespan's stops took a window of their own: {windows}"
    )


@posix_sigterm
def test_a_pending_agent_order_answers_engine_quitting_on_a_quit(tmp_path: Path) -> None:
    """A real POST /commands, waiting for a page that never claims it, at SIGTERM."""
    proc, port = _boot(tmp_path)
    try:
        _open_page(port)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
        conn.request(
            "POST", "/api/v1/commands",
            body=json.dumps({"single": {"type": "play", "deck": 1, "playing": True}}),
            headers={"Content-Type": "application/json"},
        )
        time.sleep(1.0)
        conn.sock.setblocking(False)
        try:
            with pytest.raises(BlockingIOError):
                conn.sock.recv(1)  # presence: the order is still waiting
        finally:
            conn.sock.setblocking(True)
        took, left_behind = _quit(proc)
        assert took is not None and took < QUIT_WITHIN_S, took
        assert not left_behind, left_behind
        answer = conn.getresponse()
        body = json.loads(answer.read())
        conn.close()
        assert answer.status == 503, (answer.status, body)
        assert body["reason"] == "engine_quitting", body
    finally:
        _stop(proc)


#: The real ``serve``, trying to start children once its shutdown has begun,
#: after the quit's SIGTERM reached the group, so nothing would ever signal
#: them. That is what the ahead-analysis drain did when the SIGTERM ended its
#: queue CLI mid-run, and the shell then SIGKILLed it as left behind. A
#: ``subprocess`` start must be refused outright; a ``multiprocessing`` spawn
#: raises no audit event, so it gets through and the final reap must end it.
LATE_FORK_SERVE = """
import multiprocessing, subprocess, sys, time
import apps.webui.server.routes.commands as commands

close = commands.close_order_broker

def close_and_fork(app):
    close(app)
    try:
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    except Exception as exc:
        print("popen-refused", type(exc).__name__, flush=True)
    else:
        print("popen-started", flush=True)
    child = multiprocessing.get_context("spawn").Process(target=time.sleep, args=(600,))
    child.start()
    print("late-child", child.pid, flush=True)

commands.close_order_broker = close_and_fork
from apps.engine_core.__main__ import main
if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
"""


@posix_sigterm
def test_nothing_started_during_the_quit_outlives_the_engine(tmp_path: Path) -> None:
    script = tmp_path / "late_fork_serve.py"
    # A file, not ``-c``: a spawn child re-imports its parent's main module.
    script.write_text(f"import sys\nsys.path.insert(0, {str(REPO)!r})\n" + LATE_FORK_SERVE)
    proc, _port = _boot(tmp_path, [str(script)])
    late_pid = 0
    try:
        took, _ = _quit(proc)
        assert took is not None and took < QUIT_WITHIN_S, took
        log_text = (tmp_path / "engine.log").read_text()
        assert "popen-refused SpawnAfterQuit" in log_text, (
            "if a subprocess can still start after the quit's SIGTERM then broken"
        )
        found = re.search(r"late-child (\d+)", log_text)
        assert found, "presence: the unaudited late fork never happened, so this proves nothing"
        late_pid = int(found.group(1))
        # At once, not eventually: the shell sweeps the group as soon as the
        # engine is reaped, and anything alive then is SIGKILLed as left behind.
        assert not _alive(late_pid), f"child {late_pid}, forked during the quit, outlived the engine"
    finally:
        _stop(proc)
        if late_pid and _alive(late_pid):
            os.kill(late_pid, signal.SIGKILL)


def test_a_running_engine_still_holds_a_long_poll(tmp_path: Path) -> None:
    """The overshoot control: releasing polls at shutdown must not end them at rest."""
    proc, port = _boot(tmp_path)
    try:
        _open_page(port)
        conn = _hold_long_poll(port)  # its own assertion: still held after 1 s
        conn.close()
        assert proc.poll() is None
    finally:
        _stop(proc)


#: Wedges the shutdown the way an unbounded join does, with a real child
#: process that leads its own session, so a process-group signal misses it,
#: and that IGNORES SIGTERM, so the reap has to wait out its grace and SIGKILL.
#: Ready is printed only after that child is ignoring SIGTERM and this
#: process's SIGTERM handler is installed. The test waits for that line
#: before it sends SIGTERM, so the signal cannot land in the gap and exit
#: the process before the watchdog is armed.
WEDGED_ENGINE = """
import os, signal, subprocess, sys, threading, time
from apps.shared.shutdown_bound import arm_quit_watchdog

child = subprocess.Popen(
    [sys.executable, "-c",
     "import signal, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
     "print('ready', flush=True); time.sleep(600)"],
    start_new_session=True, stdout=subprocess.PIPE,
)
assert child.stdout is not None
child_ready = child.stdout.readline()
if child_ready.strip() != b"ready":
    raise SystemExit(f"child never ignored SIGTERM: {child_ready!r}")
stop = threading.Event()

def on_term(*_):
    arm_quit_watchdog()
    stop.set()

signal.signal(signal.SIGTERM, on_term)
print(f"ready {child.pid}", flush=True)
while True:
    time.sleep(0.05)
    if stop.is_set():
        time.sleep(600)  # the wedged teardown the watchdog has to end
"""


@posix_sigterm
def test_the_watchdog_ends_a_wedged_quit_and_reaps_its_children() -> None:
    proc = subprocess.Popen(
        [sys.executable, "-c", WEDGED_ENGINE],
        cwd=REPO,
        stdout=subprocess.PIPE,
        start_new_session=True,
    )
    assert proc.stdout is not None
    ready = proc.stdout.readline()
    assert ready.startswith(b"ready "), f"the wedge was never established: {ready!r}"
    child_pid = int(ready.split()[1])
    assert psutil.pid_exists(child_pid)
    try:
        started = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=QUIT_WITHIN_S + 5)
        took = time.monotonic() - started
        # Its own deadline, not just the 3 s target: the reap is budgeted
        # inside it, so a child that ignores SIGTERM costs nothing extra.
        assert took < QUIT_DEADLINE_S + 0.2, (
            f"the watchdog took {took:.2f}s, past its {QUIT_DEADLINE_S}s deadline"
        )
        assert took >= QUIT_DEADLINE_S - REAP_BUDGET_S - 0.2, (
            f"exited after {took:.1f}s, before the watchdog's reap could start, so the "
            "teardown was not wedged and this case proves nothing (the control)"
        )
        deadline = time.monotonic() + 2.0
        while psutil.pid_exists(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(child_pid), f"child {child_pid} outlived the engine"
    finally:
        _stop(proc)
        if _alive(child_pid):
            os.kill(child_pid, signal.SIGKILL)


#: Wedged like WEDGED_ENGINE, but it forks a fresh session-leading worker
#: AFTER the watchdog's reap has taken its first look, the way a job
#: supervisor that has not been stopped yet still claims and forks.
LATE_FORKING_ENGINE = """
import signal, subprocess, sys, threading, time
from apps.shared.shutdown_bound import QUIT_DEADLINE_S, REAP_BUDGET_S, arm_quit_watchdog

WORKER = [sys.executable, "-c", "import time; time.sleep(600)"]
WITH_EARLY_CHILD = sys.argv[1] == "early"
stop = threading.Event()

def late_fork():
    time.sleep(QUIT_DEADLINE_S - REAP_BUDGET_S + 0.05)  # just after the first look
    worker = subprocess.Popen(WORKER, start_new_session=True)
    print(worker.pid, flush=True)

def on_term(*_):
    arm_quit_watchdog()
    threading.Thread(target=late_fork, daemon=True).start()
    stop.set()

# One early child that ignores SIGTERM, so the first look finds something and
# its grace wait is the window the late fork lands in.
# Without it, the first look is EMPTY, and the late fork lands while the
# reap is only watching for one.
if WITH_EARLY_CHILD:
    early = subprocess.Popen(
        [sys.executable, "-c",
         "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
         "print('ready', flush=True); time.sleep(600)"],
        start_new_session=True, stdout=subprocess.PIPE,
    )
    early.stdout.readline()
signal.signal(signal.SIGTERM, on_term)
print("armed", flush=True)
while True:
    time.sleep(0.05)
    if stop.is_set():
        time.sleep(600)
"""


@posix_sigterm
@pytest.mark.parametrize("first_look", ["early", "empty"])
def test_the_watchdog_reaps_a_worker_forked_during_its_reap(first_look: str) -> None:
    proc = subprocess.Popen(
        [sys.executable, "-c", LATE_FORKING_ENGINE, first_look],
        cwd=REPO,
        stdout=subprocess.PIPE,
        start_new_session=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == b"armed"
    late_pid = 0
    try:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=QUIT_WITHIN_S + 5)
        late_pid = int(proc.stdout.readline())  # presence: the late fork happened
        deadline = time.monotonic() + 2.0
        while _alive(late_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(late_pid), f"worker {late_pid}, forked mid-reap, outlived the engine"
    finally:
        _stop(proc)
        if late_pid and _alive(late_pid):
            os.kill(late_pid, signal.SIGKILL)


#: The engine's server class, wired the way ``serve`` wires it, in front of
#: a route that blocks the event loop with synchronous work: the case where
#: uvicorn never reaches ``shutdown()``, so only the signal handler can arm
#: the watchdog.
BLOCKED_LOOP_ENGINE = """
import sys, time, uvicorn
from apps.shared.shutdown_bound import QUIT_DEADLINE_S, arm_quit_watchdog
from apps.shared.uvicorn_shutdown import EarlyShutdownServer

async def app(scope, receive, send):
    if scope["type"] != "http":
        return
    time.sleep(600)  # synchronous work on the loop, the way a blocking scan is

server = EarlyShutdownServer(
    uvicorn.Config(app, host="127.0.0.1", port=int(sys.argv[1]), lifespan="off",
                   log_level="warning", timeout_graceful_shutdown=1),
    on_shutdown_begin=lambda: None,
    on_exit_signal=lambda: arm_quit_watchdog(QUIT_DEADLINE_S),
)
server.run()
"""


@posix_sigterm
def test_the_watchdog_bounds_a_quit_while_the_event_loop_is_blocked() -> None:
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-c", BLOCKED_LOOP_ENGINE, str(port)],
        cwd=REPO,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            assert proc.poll() is None and time.monotonic() < deadline, "never listened"
            try:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
                conn.connect()
                break
            except OSError:
                time.sleep(0.1)
        conn.request("GET", "/")  # the loop is now stuck in the handler
        time.sleep(0.5)
        assert proc.poll() is None
        started = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=QUIT_WITHIN_S + 3)
        except subprocess.TimeoutExpired:
            raise AssertionError(
                "a blocked event loop kept the engine alive past SIGTERM: "
                "the watchdog was never armed"
            ) from None
        took = time.monotonic() - started
        assert took < QUIT_DEADLINE_S + 0.2, f"took {took:.2f}s"
        conn.close()
    finally:
        _stop(proc)


#: Wedged with a thread stuck inside a real file handler's emit, holding
#: the handler's lock the way a stalled disk write does.
LOG_WEDGED_ENGINE = """
import logging, signal, sys, threading, time
from apps.shared.shutdown_bound import arm_quit_watchdog

handler = logging.FileHandler(sys.argv[1])
logging.getLogger().addHandler(handler)
logging.getLogger().setLevel(logging.INFO)
held = threading.Event()

def stuck_emit():
    handler.acquire()  # what emit() holds while its write is stalled
    held.set()
    time.sleep(600)

threading.Thread(target=stuck_emit, daemon=True).start()
held.wait()
stop = threading.Event()
signal.signal(signal.SIGTERM, lambda *_: (arm_quit_watchdog(), stop.set()))
print("armed", flush=True)
while True:
    time.sleep(0.05)
    if stop.is_set():
        time.sleep(600)
"""


@posix_sigterm
def test_the_watchdog_exits_even_when_a_log_handler_is_stuck(tmp_path: Path) -> None:
    proc = subprocess.Popen(
        [sys.executable, "-c", LOG_WEDGED_ENGINE, str(tmp_path / "engine.log")],
        cwd=REPO,
        stdout=subprocess.PIPE,
        start_new_session=True,
    )
    assert proc.stdout is not None
    try:
        assert proc.stdout.readline().strip() == b"armed"
        started = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=QUIT_WITHIN_S + 3)
        except subprocess.TimeoutExpired:
            raise AssertionError("a stuck log handler wedged the quit watchdog") from None
        took = time.monotonic() - started
        assert took < QUIT_DEADLINE_S + 0.2, f"took {took:.2f}s"
    finally:
        _stop(proc)


def _alive(pid: int) -> bool:
    """True only while ``pid`` is a live process, not a reaped zombie."""
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _slow_stop(seconds: float, started: list[str], name: str) -> tuple[str, object]:
    def stop() -> None:
        started.append(name)
        time.sleep(seconds)

    return name, stop


def test_nested_stop_groups_share_one_window_and_all_start() -> None:
    """Two groups in turn, both wedged, still finish inside ONE window, and both start."""
    state = State()
    deadline = begin_stop_window(state)
    assert begin_stop_window(state) == deadline  # opened once, then shared
    started: list[str] = []
    began = time.monotonic()
    first = run_stop_steps("outer", [_slow_stop(5.0, started, "a")], deadline=deadline)
    second = run_stop_steps("inner", [_slow_stop(5.0, started, "b")], deadline=deadline)
    took = time.monotonic() - began
    assert first == ["a"] and second == ["b"]
    assert took < STOP_STEPS_DEADLINE_S + 0.3, f"two groups took {took:.2f}s in one window"
    deadline_wait = time.monotonic() + 1.0
    while len(started) < 2 and time.monotonic() < deadline_wait:
        time.sleep(0.01)
    assert sorted(started) == ["a", "b"], "a group past the deadline was never told to stop"


def test_a_group_without_a_window_still_gets_its_own() -> None:
    """The standalone control: no shared window means the default bound, not zero."""
    finished = threading.Event()
    began = time.monotonic()
    overran = run_stop_steps("standalone", [("quick", lambda: (time.sleep(0.2), finished.set()))])
    assert overran == [] and finished.is_set(), "a 0.2 s stop was not waited for"
    assert time.monotonic() - began < STOP_STEPS_DEADLINE_S
