"""The Electron inner loop: edit, see it in the real shell, let an agent drive it.

Usage::

    just electron-dev                    # this worktree's data dir, as webui-backend uses
    just electron-dev --fixture          # a throwaway two-track library (cloud agents)
    just electron-dev --data-dir PATH    # any library you name

One command brings up, on this worktree's claimed ports:

- the reloadable FastAPI daemon (``apps.webui.server --reload``), so Python
  edits restart the API;
- Vite's dev server, so a ``.svelte`` / ``.ts`` edit reaches the window by HMR
  (measured Wed 7 Oct 2026 in the cloud: 62-170 ms, page state kept);
- the unpackaged Electron shell pointed at the Vite origin
  (``OPENDJ_ENGINE_ORIGIN``), with its CDP port open
  (``OPENDJ_REMOTE_DEBUGGING_PORT``) so ``apps/desktop/mcp/webview-mcp.ts``,
  chrome-devtools-mcp or Playwright ``connectOverCDP`` can drive it;
- a watcher on ``apps/desktop/electron/src``: a shell edit is rebuilt and the
  window relaunched, since the main process cannot hot-reload.

A backend or Vite already answering on the claimed port is reused, not
restarted, and left running on exit, except that --fixture or --data-dir
refuses to reuse a backend (R-5). What this command started, it stops, the
whole process tree on every platform (R-6).

REQUIREMENTS
  R-1 Never touch the installed app's library or profile.             [done, test]
      [if the plan omits --user-data-dir or OPENDJ_SHELL_DATA_DIR then
       the dev window shares the installed Open DJ's single-instance lock
       (it exits at once) and writes its logs into that app's library]
  R-2 One isolated window per worktree.                                [done, test]
      [if two worktrees derive the same CDP port or profile dir then
       agents drive each other's window; the profile is keyed by the
       worktree's path too, since a released port is claimed again]
  R-3 Headless on a Linux box with no display.                         [done, test]
      [if DISPLAY and WAYLAND_DISPLAY are unset on Linux then the shell runs
       under xvfb-run, else under nothing]
  R-4 Root (cloud containers) adds --no-sandbox, nobody else does.     [done, test]
  R-5 An explicit library is the one the window gets.                 [done, test]
      [if --fixture or --data-dir is given and a backend already answers
       then refuse, since that backend serves whatever it was started with]
  R-6 Stopping ends whole process trees.                              [done, test]
      [POSIX signals the child's process group; Windows runs taskkill /T]
  R-7 A reused server is this worktree's, and a dead one stops the run. [done, test]
      [a server is reused only when it serves this library AND its listening
       process runs from this checkout (unreadable means refused); a backend
       or Vite this command started that exits ends the run with status 1,
       and so does a started backend whose /api/v1/health stays down past
       BACKEND_DOWN_GRACE_S, the uvicorn reload worker having died under a
       still-running reloader]
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil

from apps.shared.process_groups import (
    GROUPS_SUPPORTED,
    group_alive,
    group_gone_within,
    group_held_by,
    live_group_members,
    stop_tree,
)
from scripts.server_owner_guard import process_namespace

REPO_ROOT = Path(__file__).resolve().parents[1]
ELECTRON_DIR = REPO_ROOT / "apps" / "desktop" / "electron"
FRONTEND_DIR = REPO_ROOT / "apps" / "webui" / "frontend"
FIXTURE_MODULE = "apps.webui.frontend.tests.e2e.support.deckload_fixture"

#: CDP port = the worktree's claimed frontend port plus this, so every
#: worktree gets its own (frontend ports are unique per worktree).
CDP_PORT_OFFSET = 20000
XVFB_SCREEN = "1920x1200x24"
WATCH_POLL_S = 0.5
#: A started backend whose health stays down this long ends the run (R-7): long
#: enough for a uvicorn reload to restart its worker, short enough to fail fast.
BACKEND_DOWN_GRACE_S = 20.0
BACKEND_PROBE_S = 2.0
READY_TIMEOUT_S = 180.0


@dataclass(frozen=True)
class LaunchPlan:
    """How to start the Electron window. Pure data, so the guards are testable."""

    argv: list[str]
    env: dict[str, str]
    state_dir: Path
    cdp_port: int


def state_dir_for(frontend_port: int, tmp_root: Path | None = None, *, worktree: Path = REPO_ROOT) -> Path:
    """Per-worktree profile and shell-data root, stable across runs (R-2).

    Keyed by the worktree's resolved path as well as its port: a released port
    is later claimed by another worktree, which must not inherit this one's
    Chromium profile and local storage (Sol on #6830).
    """
    root = Path(tempfile.gettempdir()) if tmp_root is None else tmp_root
    tag = hashlib.sha256(str(worktree.resolve()).encode()).hexdigest()[:12]
    return root / f"opendj-electron-dev-{frontend_port}-{tag}"


def needs_virtual_display(platform: str, environ: dict[str, str]) -> bool:
    """R-3: only Linux without any display server gets xvfb-run."""
    if not platform.startswith("linux"):
        return False
    return not (environ.get("DISPLAY") or environ.get("WAYLAND_DISPLAY"))


def plan_launch(
    *,
    electron_binary: str,
    backend_port: int,
    frontend_port: int,
    environ: dict[str, str],
    platform: str,
    is_root: bool,
    xvfb_run: str | None,
    tmp_root: Path | None = None,
) -> LaunchPlan:
    """The Electron command line and environment for one worktree."""
    state = state_dir_for(frontend_port, tmp_root)
    cdp_port = frontend_port + CDP_PORT_OFFSET
    if cdp_port > 65535:
        raise SystemExit(
            f"[ERROR] frontend port {frontend_port} leaves no CDP port at +{CDP_PORT_OFFSET} "
            f"({cdp_port} > 65535); claim a frontend port at or below {65535 - CDP_PORT_OFFSET}"
        )
    # The claimed pair, never the one Just exported from `.env` before claim_ports() ran.
    env = with_ports(dict(environ), backend=backend_port, frontend=frontend_port)
    env["OPENDJ_ENGINE_ORIGIN"] = f"http://127.0.0.1:{frontend_port}"
    env["OPENDJ_REMOTE_DEBUGGING_PORT"] = str(cdp_port)
    # R-1: logs and shell health go here, never into the installed app's library.
    env["OPENDJ_SHELL_DATA_DIR"] = str(state / "shell-data")
    argv: list[str] = []
    if needs_virtual_display(platform, environ):
        if xvfb_run is None:
            raise SystemExit("[ERROR] no display and no xvfb-run on PATH: install xvfb, or set DISPLAY")
        argv += [xvfb_run, "-a", "-s", f"-screen 0 {XVFB_SCREEN}"]
    argv.append(electron_binary)
    if is_root:
        argv.append("--no-sandbox")  # R-4: Chromium refuses to sandbox as root
    # R-1/R-2: its own profile, so its own single-instance lock and storage.
    argv.append(f"--user-data-dir={state / 'user-data'}")
    argv.append(str(ELECTRON_DIR))
    return LaunchPlan(argv=argv, env=env, state_dir=state, cdp_port=cdp_port)


# ----- process plumbing -------------------------------------------------------


def _answers(url: str) -> bool:
    """Whether `url` answers with a success (2xx). A 4xx or 5xx is a missing or rejected
    endpoint, never a live service (Sol on #6830)."""
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError):
        return False


def _health_db(origin: str) -> str | None:
    """The state.db path the backend behind `origin` reports, or None when it does not say."""
    try:
        with urllib.request.urlopen(f"{origin}/api/v1/health", timeout=5) as response:
            payload = json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None
    path = payload.get("state_db", {}).get("path") if isinstance(payload, dict) else None
    return path if isinstance(path, str) else None


def reuse_mismatch(what: str, origin: str, reported_db: str | None, expected_db: Path) -> str | None:
    """Why the server already on `origin` is not this worktree's, or None when it is.

    A port answering proves only that something listens there. The state.db a
    backend reports (directly, or through Vite's /api proxy) names the library
    it serves, so it is the identity a reuse must match.
    """
    if reported_db is None:
        return (
            f"[ERROR] something answers on {origin} but its /api/v1/health names no state.db, so it is not "
            f"this worktree's {what}. Stop it, or free the port."
        )
    if Path(reported_db).resolve() != expected_db.resolve():
        return (
            f"[ERROR] the {what} on {origin} serves {reported_db}, not this worktree's {expected_db}. "
            "Stop it, or free the port."
        )
    return None


def checkout_root(path: Path) -> Path | None:
    """The nearest directory at or above `path` holding a `.git` entry: the checkout it belongs to."""
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def ownership_mismatch(what: str, origin: str, cwds: list[Path] | None, worktree: Path) -> str | None:
    """Why the server on `origin` cannot be proven this worktree's, or None when it is.

    Two worktrees may serve the same library, so a matching state.db does not
    say whose checkout the server runs. The listening processes' working
    directories do: each must sit in this checkout and no other (Sol on #6830).
    Unmeasurable is a refusal, never a pass.
    """
    if not cwds:
        return (
            f"[ERROR] cannot tell which checkout the {what} on {origin} runs from (its listening process "
            "is not readable here), so it is not reused. Stop it, or free the port."
        )
    others = sorted({str(cwd) for cwd in cwds if checkout_root(cwd) != worktree.resolve()})
    if others:
        return (
            f"[ERROR] the {what} on {origin} runs from {', '.join(others)}, not this worktree ({worktree}). "
            "Stop it, or free the port."
        )
    return None


def _listener_cwds(port: int) -> list[Path] | None:
    """The working directories of the processes listening on `port`, or None when unreadable."""
    cwds: list[Path] = []
    for proc in psutil.process_iter():
        try:
            listening = any(
                conn.status == psutil.CONN_LISTEN and conn.laddr and conn.laddr.port == port
                for conn in proc.net_connections(kind="tcp")
            )
            if listening:
                cwds.append(Path(proc.cwd()).resolve())
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except psutil.AccessDenied:
            continue  # another user's process: if it is the listener, cwds stays empty and reuse is refused
    return cwds or None


def _refuse_unless_ours(what: str, origin: str, expected_db: Path, port: int) -> None:
    mismatch = reuse_mismatch(what, origin, _health_db(origin), expected_db)
    if mismatch is None:
        mismatch = ownership_mismatch(what, origin, _listener_cwds(port), REPO_ROOT)
    if mismatch is not None:
        raise SystemExit(mismatch)


def _backend_state_db(origin: str) -> Path:
    reported = _health_db(origin)
    if reported is None:
        raise SystemExit(f"[ERROR] the backend on {origin} names no state.db in /api/v1/health")
    return Path(reported)


def _expected_state_db() -> Path:
    # The same constant a backend started here would bind (MDT_DATA_DIR-aware).
    from apps.adapters.rekordbox.config import STATE_DB

    return STATE_DB


def _wait_for(url: str, what: str, started: subprocess.Popen[bytes] | None) -> None:
    deadline = time.monotonic() + READY_TIMEOUT_S
    while time.monotonic() < deadline:
        if started is not None:
            # Before the probe and before poll() can reap a leader that died
            # mid-startup: its helpers are then still found by _stop (Codex on #6830).
            remember_tree(started)
        if _answers(url):
            return
        if started is not None and reap_exited(started) is not None:
            raise SystemExit(f"[ERROR] {what} exited with {started.returncode} before answering {url}")
        time.sleep(0.5)
    raise SystemExit(f"[ERROR] {what} did not answer {url} within {READY_TIMEOUT_S:.0f} s")


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if os.name == "posix":
            # A just-closed shell leaves TIME_WAIT on the port, which Chromium's own
            # listener binds through; only a live listener counts as taken. Not on
            # Windows, where SO_REUSEADDR would bind over a live listener too.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _find(command: str, search: str | None = None) -> str | None:
    """`command`'s absolute path from the absolute entries of PATH (or `search`), or None.

    A relative entry such as `.` or `node_modules/.bin` would resolve against
    whatever directory a child is later started in, where the file can be missing
    or a checkout-local shadow (Codex on #6830), so it is never searched, and a
    result that is still relative is refused.
    """
    entries = (os.environ.get("PATH", "") if search is None else search).split(os.pathsep)
    absolute = os.pathsep.join(entry for entry in entries if entry and os.path.isabs(entry))
    found = shutil.which(command, path=absolute) if absolute else None
    return found if found is not None and os.path.isabs(found) else None


def _resolve(command: str, search: str | None = None) -> str:
    """`command`'s absolute path as `_find` resolves it; a missing tool ends the run."""
    found = _find(command, search)
    if found is None:
        raise SystemExit(f"[ERROR] {command} is not on PATH (only absolute PATH entries are searched)")
    return found


def _electron_binary() -> str:
    node = _resolve("node")
    result = subprocess.run(
        [node, "-p", "require('electron')"], cwd=ELECTRON_DIR, capture_output=True, text=True, check=False
    )
    path = result.stdout.strip()
    if result.returncode != 0 or not path or not Path(path).exists():
        raise SystemExit(
            "[ERROR] the Electron runtime is missing: "
            f"cd {ELECTRON_DIR.relative_to(REPO_ROOT)} && pnpm install --frozen-lockfile\n{result.stderr}"
        )
    return path


@dataclass(frozen=True)
class ShellBuild:
    """How the watch loop rebuilds the shell, and its sources as of the build the window runs."""

    argv: list[str]
    shell_dir: Path
    stamp: frozenset[tuple[str, float]]
    env: dict[str, str]


def shell_build_argv(pnpm: str) -> list[str]:
    """The command that compiles the shell, run in the shell's directory."""
    return [pnpm, "exec", "tsc", "-p", "tsconfig.json"]


def _run_setup(argv: list[str], what: str, *, service: str, cwd: Path, env: dict[str, str]) -> int:
    """Run a setup command to completion through the same tree lifecycle as the services; its exit code.

    A Ctrl+C or SIGTERM while it runs stops its descendants too (pnpm's node/tsc,
    the fixture builder's uv children), rather than leaving them writing after
    this command exits (Codex on #6830). `env` is required, so a setup child gets
    the claimed ports like every service does, never the pair Just exported (Sol P1 on #6830).
    """
    spawned: list[subprocess.Popen[bytes]] = []
    try:
        return _spawn(argv, service=service, cwd=cwd, env=env, register=spawned.append).wait()
    except BaseException:
        for process in spawned:
            _stop(process, what)
        raise


def _build_shell(build: list[str], shell_dir: Path, env: dict[str, str]) -> bool:
    started = time.monotonic()
    if _run_setup(build, "shell build", service="shell-build", cwd=shell_dir, env=env) != 0:
        print("[ERROR] shell TypeScript did not compile", flush=True)
        return False
    print(f"[OK] shell built in {time.monotonic() - started:.1f} s", flush=True)
    return True


def shell_sources_stamp(src: Path) -> frozenset[tuple[str, float]]:
    """Every shell source with its mtime, so an add, edit or delete each changes it.

    A max-mtime stamp missed deleting any file but the newest one.
    """
    stamp: set[tuple[str, float]] = set()
    for path in src.rglob("*.ts"):
        try:
            stamp.add((str(path), path.stat().st_mtime))
        except FileNotFoundError:
            continue  # gone between listing and stat (an atomic save): its absence is the change
    return frozenset(stamp)


def window_exit_status(returncode: int) -> int:
    """The recipe's exit status once the window closes: a crash must not read as success."""
    return 0 if returncode == 0 else 1


def child_env(environ: dict[str, str], service: str, namespace: str) -> dict[str, str]:
    """A child's environment carrying its own AF_SERVICE_ID, never an inherited one.

    `.dev.` rather than `.test.`: these are a developer's live servers, which the
    orphan reaper must not collect; the id makes a survivor attributable.
    """
    return {**environ, "AF_SERVICE_ID": f"{namespace}.dev.electron-dev.{service}"}


#: POSIX: every child leads its own process group, so stopping it also stops
#: what it spawned (uvicorn's reloader worker, esbuild, Electron's helpers,
#: the app under xvfb-run). Windows has no process groups and stops the tree
#: with taskkill instead; both arms live in apps/shared/process_groups.py.
_GROUPS = GROUPS_SUPPORTED


@contextlib.contextmanager
def _stops_deferred() -> Iterator[None]:
    """Hold Ctrl+C and SIGTERM until the block ends, then deliver the first one that came.

    A stop that lands between a child's creation and its registration would unwind
    to a cleanup that has no handle on it, leaving that tree running (Sol P1 on
    #6830). Only Python's handlers are swapped, so the child starts with the default
    dispositions, never a blocked mask. Off the main thread, where handlers cannot
    be set, nothing is deferred (the watch loop runs on one only in tests).
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    caught: list[signal.Signals] = []
    stops = (signal.SIGINT, signal.SIGTERM)
    previous = {sig: signal.signal(sig, lambda signum, _frame: caught.append(signal.Signals(signum))) for sig in stops}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        if caught:
            handler = previous[caught[0]]
            if callable(handler):
                handler(caught[0], None)
            elif handler == signal.SIG_DFL:
                signal.raise_signal(caught[0])


def _spawn(
    argv: list[str],
    *,
    service: str,
    env: dict[str, str] | None = None,
    register: Callable[[subprocess.Popen[bytes]], object] | None = None,
    **kwargs: object,
) -> subprocess.Popen[bytes]:
    """Start a child and hand it to `register` before any stop can interrupt in between."""
    child = child_env(dict(os.environ) if env is None else env, service, process_namespace(REPO_ROOT))
    with _stops_deferred():
        spawned_at = time.time()
        process = subprocess.Popen(argv, start_new_session=_GROUPS, env=child, **kwargs)  # type: ignore[call-overload]
        # The child's own start time, on the clock its descendants' start times are read
        # from: Linux derives those from boot time, which can sit a second off time.time().
        with contextlib.suppress(psutil.Error):
            spawned_at = psutil.Process(process.pid).create_time()
        _SPAWNED_AT[process.pid] = spawned_at
        if register is not None:
            register(process)
    return process


#: When each child was spawned, by pid: the floor below which a process naming a reaped
#: leader as its parent cannot be one that leader started.
_SPAWNED_AT: dict[int, float] = {}


def orphans_of(
    leader_pid: int, since: float, exclude: set[int], procs: Iterable[dict[str, Any]] | None = None
) -> list[int]:
    """Live descendants of `leader_pid` by recorded parent pid that started at or after `since`.

    Windows never reparents a child whose parent exits, so after the leader is reaped
    this is the only trace of a descendant it started after the last snapshot, nested
    ones included (Sol P1 on #6830). Excluded (tracked) processes are walked through
    but not reported.
    """
    if procs is None:
        procs = (proc.info for proc in psutil.process_iter(["pid", "ppid", "create_time"]))
    children: dict[int, list[tuple[int, float]]] = {}
    for info in procs:
        started = info.get("create_time") or 0.0
        if started >= since and info.get("pid") != leader_pid:
            children.setdefault(info.get("ppid") or 0, []).append((info["pid"], started))
    found: set[int] = set()
    frontier = [(leader_pid, since)]
    while frontier:
        parent, parent_started = frontier.pop()
        for pid, started in children.get(parent, []):
            # A child older than the process now holding its parent's pid was started by
            # an earlier holder of that pid, so it is not part of this tree.
            if pid not in found and started >= parent_started:
                found.add(pid)
                frontier.append((pid, started))
    return sorted(found - exclude)


def _signal(process: subprocess.Popen[bytes], *, force: bool) -> None:
    failure = stop_tree(process.pid, force=force, kill_leader=process.kill)
    if failure:
        # The tree stop is the only thing that reaches a descendant started after the
        # snapshot, so not reaching the tree fails the cleanup (Sol P1 on #6830).
        _cleanup_failed(f"[ERROR] {failure}; an untracked descendant may still be running, stop it by hand")


def _signal_pinned(
    process: subprocess.Popen[bytes], tracked: list[psutil.Process], *, force: bool, groups: bool
) -> bool:
    """Signal `process`'s tree by its leader's pid, only while that pid provably names it; whether it was sent.

    An unreaped leader keeps its pid, so it is signalled as is. A reaped one's
    group id stays this run's only while a tracked member holds it, and a check
    followed by a signal is not atomic: the member could exit in between and free
    the id for an unrelated group (Sol P1 on #6830). So the member is stopped
    first, re-checked, and resumed only after the group signal has been sent.
    """
    if reap_exited(process, groups=groups) is None:
        _signal(process, force=force)
        return True
    if not groups:
        return False
    # A leader reaped just now had its group's members remembered first; they hold the id too.
    for proc in [*tracked, *_REMEMBERED.get(process.pid, {}).values()]:
        if not group_held_by(process.pid, [proc]):
            continue
        try:
            proc.suspend()  # psutil refuses a pid reused since it was tracked
        except psutil.NoSuchProcess:
            continue
        try:
            # SIGSTOP lands asynchronously, so the member is re-checked only once it has
            # stopped; stopped, it cannot leave the group, so the id stays reserved.
            if _stopped_within(proc, PIN_STOP_TIMEOUT_S) and group_held_by(process.pid, [proc]):
                _signal(process, force=force)
                return True
        finally:
            with contextlib.suppress(psutil.NoSuchProcess):
                proc.resume()
    return False


#: How long `_signal_pinned` waits for a member it suspended to report stopped.
PIN_STOP_TIMEOUT_S = 1.0


def _stopped_within(proc: psutil.Process, timeout_s: float) -> bool:
    """Whether `proc` reports stopped within `timeout_s`; False once it is gone."""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            if proc.status() == psutil.STATUS_STOPPED:
                return True
        except psutil.NoSuchProcess:
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.005)


#: How long a stopped tree gets to exit after SIGTERM, and again after SIGKILL.
STOP_GRACE_S = 10.0

#: Cleanup failures this run, so the command exits nonzero after them.
_CLEANUP_FAILURES: list[str] = []


def _cleanup_failed(message: str) -> None:
    _CLEANUP_FAILURES.append(message)
    print(message, flush=True)


def exit_status(main_status: int, cleanup_failures: list[str]) -> int:
    """The command's exit status: a failed cleanup turns a clean exit into 1."""
    return main_status or (1 if cleanup_failures else 0)


def _tree(pid: int) -> list[psutil.Process]:
    """The process and every descendant, taken before anything is signalled."""
    try:
        root = psutil.Process(pid)
        return [root, *root.children(recursive=True)]
    except psutil.NoSuchProcess:
        return []


#: Every process seen in each started tree, by leader pid. Refreshed before the
#: watch loop polls a leader, because polling reaps an exited leader and then
#: neither `_tree()` nor `taskkill /T` can find what it left behind (Codex and
#: Sol on #6830). POSIX also signals the process group, which outlives its leader.
_REMEMBERED: dict[int, dict[int, psutil.Process]] = {}


def _live_tree(process: subprocess.Popen[bytes]) -> list[psutil.Process]:
    """`process`'s tree, or nothing once it has been reaped.

    An unreaped child keeps its pid, even as a zombie, so the pid is still ours
    while `returncode` is None. After the reap the pid can name an unrelated process,
    and walking it would hand that process's tree to `_stop` (Codex on #6830).
    This never polls: a poll would reap a leader that exited since the last snapshot
    before its tree was read, losing what it started meanwhile (Sol P1 on #6830).
    The callers poll after it.
    """
    return _tree(process.pid) if process.returncode is None else []


def remember_tree(process: subprocess.Popen[bytes]) -> None:
    """Add `process`'s current descendants to what `_stop` will wait on and kill."""
    known = _REMEMBERED.setdefault(process.pid, {})
    for proc in _live_tree(process):
        known.setdefault(proc.pid, proc)


def reap_exited(process: subprocess.Popen[bytes], *, groups: bool = _GROUPS) -> int | None:
    """`process.poll()`, except that an exited POSIX leader's group is remembered before the reap.

    Until it is reaped, an exited leader is a zombie that still holds its pid and
    its process-group id, so every live member of that group is provably this
    run's, a helper it started after the last `remember_tree` included. Once
    reaped, the id names this run's group only while a tracked member holds it,
    so a helper nothing tracked would be left running (Sol P1 on #6830). So a
    leader is reaped only after it is seen to have exited and its group has been
    remembered; one still running is not polled at all. Without groups the leader
    is polled as is: the open process handle keeps its pid from being reused, and
    `_stop` finds what it left by parent pid.
    """
    if process.returncode is not None or not groups:
        return process.poll()
    try:
        if psutil.Process(process.pid).status() != psutil.STATUS_ZOMBIE:
            return None
    except psutil.NoSuchProcess:
        return process.poll()
    remember_tree(process)
    known = _REMEMBERED.setdefault(process.pid, {})
    for proc in live_group_members(process.pid):
        known.setdefault(proc.pid, proc)
    return process.poll()


def _tracked(process: subprocess.Popen[bytes]) -> list[psutil.Process]:
    """The live tree plus every remembered descendant, each process once.

    A remembered entry wins over a fresh one with the same pid: psutil pins it
    to the create time it was seen with, so a reused pid cannot replace it.
    """
    merged = dict(_REMEMBERED.pop(process.pid, {}))
    for proc in _live_tree(process):
        merged.setdefault(proc.pid, proc)
    return list(merged.values())


def _wait_gone(procs: list[psutil.Process], timeout_s: float) -> list[psutil.Process]:
    """The processes in `procs` still running after up to `timeout_s`; a zombie counts as gone.

    Polled rather than `psutil.wait_procs`: on Linux that opens a pidfd per process,
    and `pidfd_open` answers EINVAL for one caught mid-reap by its new parent, which
    a helper reparented to init can be right after the SIGTERM (seen on CI for #6830).
    Polling also leaves the direct child's reap to `Popen.wait`, which keeps its exit code.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        alive = [proc for proc in procs if _running(proc)]
        if not alive or time.monotonic() >= deadline:
            return alive
        time.sleep(0.05)


def survivors_message(what: str, alive: list[int]) -> str | None:
    """The cleanup failure for processes still alive after a kill, or None when all are gone."""
    if not alive:
        return None
    pids = ", ".join(str(pid) for pid in sorted(alive))
    return f"[ERROR] {what} left process(es) {pids} alive after a kill; they may hold ports, stop them by hand"


def _stop(
    process: subprocess.Popen[bytes] | None,
    what: str,
    *,
    grace_s: float = STOP_GRACE_S,
    groups: bool = _GROUPS,
) -> None:
    """End `process` and its whole tree, judged by which tracked processes are still alive.

    The tree is snapshotted first, so a descendant that outlives its parent
    (a uvicorn reload worker, an Electron helper) is still waited on and
    killed, and so is one remembered by `remember_tree` whose leader has
    already exited. On POSIX the process group is signalled too, which also
    reaches a descendant started after the snapshot; without groups each
    tracked process is signalled directly.
    """
    if process is None:
        return
    reap_exited(process, groups=groups)
    tracked = _tracked(process)
    # The leader's pid, and with it the group id and `taskkill /T` root, is only
    # this run's while it is provably so; otherwise each tracked process is
    # signalled on its own, which psutil checks against its start time.
    by_leader = _leader_is_ours(process, tracked, groups=groups) and _signal_pinned(
        process, tracked, force=False, groups=groups
    )
    # A leader that exited during the checks above had its group remembered as it was reaped.
    seen = {proc.pid for proc in tracked}
    tracked += [proc for pid, proc in _REMEMBERED.pop(process.pid, {}).items() if pid not in seen]
    # A reaped leader whose group nothing tracked holds any more: members it started
    # after the last snapshot are out of reach, since the id cannot be proven ours.
    unreached = groups and not by_leader and process.poll() is not None
    # Without groups, a reaped leader leaves no tree to walk for what it started after
    # the last snapshot; its children still name it as parent, so they are stopped below.
    orphaned = not groups and process.poll() is not None
    if not (by_leader and groups):
        for proc in tracked:
            with contextlib.suppress(psutil.NoSuchProcess):
                proc.terminate()
    alive = _wait_gone(tracked, grace_s)
    alive = [proc for proc in alive if _running(proc)]
    group_left = by_leader and _group_alive(process.pid)
    unproven = False
    if alive or group_left:
        print(f"[WARN] {what} ignored SIGTERM for {grace_s:.0f} s; killing it", flush=True)
        if by_leader:
            # The group must still be proven ours when the kill goes out, not just at the start.
            by_leader = _signal_pinned(process, tracked, force=True, groups=groups)
            # Losing that proof with members still in the group means they were never
            # killed, so it is a failure, never a reason to skip the check (Codex on #6830).
            unproven = group_left and not by_leader
        for proc in alive:
            with contextlib.suppress(psutil.NoSuchProcess):
                proc.kill()
        alive = _wait_gone(alive, grace_s)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=1)  # reap the direct child so it is not left a zombie
    failure = survivors_message(what, [proc.pid for proc in alive if _running(proc)])
    # The group gets the same grace to empty as the tracked processes did: a member
    # spawned after the snapshot is still dying from the SIGKILL above, not surviving it.
    if failure is None and by_leader and not group_gone_within(process.pid, grace_s):
        failure = f"[ERROR] {what}'s process group {process.pid} survived a kill; stop it by hand"
    # They are never signalled, but they are looked for once the tracked tree is gone,
    # so a run that left one holding a port does not report a clean stop (Sol P1 on #6830).
    if failure is None and unreached and not group_gone_within(process.pid, grace_s, groups=groups):
        failure = (
            f"[ERROR] {what} exited before its process group {process.pid} was proven this run's, and the group "
            "still has members nothing tracked; they were not signalled, stop them by hand if they are this run's"
        )
    if failure is None and orphaned:
        failure = _stop_orphans(process, what, tracked, grace_s)
    if failure is None and unproven:
        failure = (
            f"[ERROR] {what}'s process group {process.pid} still had members after SIGTERM, but nothing this run "
            "tracked held it any more, so it was not killed; stop them by hand"
        )
    if failure:
        _cleanup_failed(failure)


def _stop_orphans(
    process: subprocess.Popen[bytes], what: str, tracked: list[psutil.Process], grace_s: float
) -> str | None:
    """Stop the live descendants a leader reaped without groups left untracked; the failure, or None.

    The open `Popen` handle keeps the leader's pid from being reused, so a process
    naming it as parent that started after the spawn was started by the leader
    (Sol P1 on #6830). Each is pinned to the start time it was found with before
    it is signalled, so a pid reused since the listing is left alone.
    """
    since = _SPAWNED_AT.get(process.pid, 0.0)
    left: list[psutil.Process] = []
    for pid in orphans_of(process.pid, since, {proc.pid for proc in tracked}):
        with contextlib.suppress(psutil.NoSuchProcess):
            proc = psutil.Process(pid)
            if proc.create_time() >= since:
                left.append(proc)
    if not left:
        return None
    print(f"[WARN] {what} exited before it was stopped; stopping what it left: {[p.pid for p in left]}", flush=True)
    for proc in left:
        with contextlib.suppress(psutil.NoSuchProcess):
            proc.terminate()
    alive = _wait_gone(left, grace_s)
    for proc in alive:
        with contextlib.suppress(psutil.NoSuchProcess):
            proc.kill()
    alive = _wait_gone(alive, grace_s)
    return survivors_message(what, [proc.pid for proc in alive if _running(proc)])


def _leader_is_ours(process: subprocess.Popen[bytes], tracked: list[psutil.Process], *, groups: bool) -> bool:
    """Whether signalling by the leader's pid (its process group, or `taskkill /T`) still reaches only this run.

    Until the leader is reaped its pid cannot be reused, so the answer is yes.
    Once reaped, its number is free: on POSIX it stays reserved only while a
    member of its group holds it, so the group is still ours exactly when a
    tracked process (identity-checked by psutil against its start time) is in
    it. Otherwise the number may now name an unrelated process or group, and
    signalling it could kill someone else's work (Sol P1 on #6830).
    """
    if reap_exited(process, groups=groups) is None:
        return True
    return group_held_by(process.pid, [*tracked, *_REMEMBERED.get(process.pid, {}).values()], groups=groups)


def _running(proc: psutil.Process) -> bool:
    """Alive and not a zombie: a zombie holds no ports, it only awaits its parent's reap."""
    try:
        # is_running() also rejects a pid reused since `proc` was taken (start time differs).
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _group_alive(pgid: int) -> bool:
    """Whether any live (non-zombie) process is still in process group `pgid`."""
    return group_alive(pgid)


def library_mode_for(*, platform: str, current: str | None, explicit_library: bool) -> str | None:
    """The MDT_LIBRARY_MODE to set for the backend, or None to leave the environment alone.

    The engine requires an explicit mode off macOS (apps/shared/library_mode.py).
    Choosing `local` is safe only for a library this command was told to use
    (`--fixture` or `--data-dir`); for the worktree's own library a missing
    mode is the operator's decision, so it stops the run instead of guessing.
    """
    if platform.startswith("darwin") or current:
        return None
    if explicit_library:
        return "local"
    raise SystemExit(
        "[ERROR] MDT_LIBRARY_MODE is unset and this is not macOS. Set it (e.g. MDT_LIBRARY_MODE=local) to run "
        "against this worktree's library, or pass --fixture for a throwaway one."
    )


def backend_reuse_refusal(*, backend_up: bool, explicit_source: bool, origin: str) -> str | None:
    """Why a running backend must not be reused, or None when reuse is safe.

    A backend already on this worktree's port serves whatever library it was
    started with. Reusing it after `--fixture` or `--data-dir` would point the
    agent at that library, possibly a real one, under a throwaway assumption.
    """
    if backend_up and explicit_source:
        return (
            f"[ERROR] a backend already answers on {origin}, and it serves the library it was started with, "
            "not the one --fixture/--data-dir asked for. Stop it first, or drop the flag to reuse it."
        )
    return None


def _fixture_data_dir(frontend_port: int, env: dict[str, str]) -> Path:
    data_dir = state_dir_for(frontend_port) / "fixture-library"
    argv = [sys.executable, "-m", FIXTURE_MODULE, "--data-dir", str(data_dir), "--seed-playlists"]
    code = _run_setup(argv, "fixture builder", service="fixture-build", cwd=REPO_ROOT, env=env)
    if code != 0:
        raise SystemExit(f"[ERROR] the fixture builder exited {code}")
    return data_dir


def with_ports(environ: dict[str, str], *, backend: int, frontend: int) -> dict[str, str]:
    """`environ` carrying the pair claim_ports() returned, never the pair inherited from Just.

    `set dotenv-load` exports the `.env` Just read before claim_ports() could
    rewrite it, so a copied or stale `.env` would otherwise reach Vite (which
    then refuses its own port check) and the backend (whose origin guard then
    rejects the newly claimed Vite origin) (Codex and Sol on #6830).
    """
    from apps.webui.port_config import BACKEND_ENV, FRONTEND_ENV

    return {**environ, BACKEND_ENV: str(backend), FRONTEND_ENV: str(frontend)}


def _backend_env(
    args: argparse.Namespace, *, backend_port: int, frontend_port: int, backend_up: bool
) -> dict[str, str]:
    """The environment a backend this run starts gets: the claimed ports, the library named and, off macOS, its mode."""
    env = with_ports(dict(os.environ), backend=backend_port, frontend=frontend_port)
    if args.fixture:
        env["MDT_DATA_DIR"] = str(_fixture_data_dir(frontend_port, dict(env)))
    elif args.data_dir is not None:
        env["MDT_DATA_DIR"] = str(args.data_dir.resolve())
    # Only a backend this run starts needs a mode; a reused one already has its own.
    mode = (
        None
        if backend_up
        else library_mode_for(
            platform=sys.platform,
            current=env.get("MDT_LIBRARY_MODE"),
            explicit_library=args.fixture or args.data_dir is not None,
        )
    )
    if mode is not None:
        env["MDT_LIBRARY_MODE"] = mode
        print(f"[INFO] MDT_LIBRARY_MODE unset off macOS; using {mode} for the library you named", flush=True)
    return env


def dead_service(started: list[tuple[subprocess.Popen[bytes], str]]) -> str | None:
    """The error for the first owned backend or Vite process that has exited, or None while all run.

    The window keeps running against a dead page otherwise, and Ctrl+C would
    later exit 0 over a failed service (Sol on #6830).
    """
    for process, what in started:
        if reap_exited(process) is not None:
            return f"[ERROR] the {what} this command started exited with {process.returncode}; stopping"
    return None


def backend_stall(
    answered: bool, down_since: float | None, now: float, grace_s: float = BACKEND_DOWN_GRACE_S
) -> tuple[float | None, str | None]:
    """(when the backend went down, or None while it answers; the error once it has been down past grace_s).

    The reload parent stays alive when an edit leaves a module that fails to
    import, so polling the process alone misses a dead API (Sol on #6830).
    """
    if answered:
        return None, None
    since = now if down_since is None else down_since
    if now - since < grace_s:
        return since, None
    return since, (
        f"[ERROR] the backend this command started has not answered /api/v1/health for {now - since:.0f} s "
        "(its reload worker likely failed to import); stopping"
    )


class _BackendProbe:
    """Probes a started backend's health every BACKEND_PROBE_S and reports a stall past the grace."""

    def __init__(self, url: str | None) -> None:
        self._url = url
        self._down_since: float | None = None
        self._next = 0.0

    def error(self) -> str | None:
        now = time.monotonic()
        if self._url is None or now < self._next:
            return None
        self._next = now + BACKEND_PROBE_S
        self._down_since, message = backend_stall(_answers(self._url), self._down_since, now)
        return message


def _watch_shell(
    windows: list[subprocess.Popen[bytes]],
    plan: LaunchPlan,
    *,
    cdp: str,
    shell: ShellBuild,
    no_watch: bool,
    started: list[tuple[subprocess.Popen[bytes], str]],
    backend_health: str | None = None,
) -> int:
    """Relaunch the window on shell edits until it closes; `windows` holds the live one.

    A rebuild that fails stops the window rather than leaving the previous build
    up, so nothing can drive stale code over CDP; the next edit that compiles
    relaunches it. While it is stopped `windows` is empty, so the cleanup never
    signals a reaped window whose pid may since have been reused (Codex on #6830).
    `shell.stamp` is the sources as they were before the build the window runs, so
    an edit saved while that build or the window's startup ran still relaunches it.
    """
    shell_src = shell.shell_dir / "src"
    stamp = shell.stamp
    probe = _BackendProbe(backend_health)
    while True:
        time.sleep(WATCH_POLL_S)
        for process in [*windows, *(process for process, _ in started)]:
            remember_tree(process)
        died = dead_service(started) or probe.error()
        if died is not None:
            print(died, flush=True)
            return 1
        if windows and reap_exited(windows[0]) is not None:
            window = windows[0]
            status = window_exit_status(window.returncode)
            tag = "[OK]" if status == 0 else "[ERROR]"
            print(f"{tag} the window closed (exit {window.returncode}); stopping", flush=True)
            return status
        if no_watch:
            continue
        latest = shell_sources_stamp(shell_src)
        if latest == stamp:
            continue
        stamp = latest
        print("[INFO] shell source changed; rebuilding", flush=True)
        if not _build_shell(shell.argv, shell.shell_dir, shell.env):
            if windows:
                _stop(windows.pop(), "Electron")
            print("[ERROR] window stopped until the shell compiles; fix the source to relaunch", flush=True)
            continue
        relaunch_started = time.monotonic()
        if windows:
            _stop(windows.pop(), "Electron")
        _spawn(plan.argv, env=plan.env, service="electron", register=windows.append)
        _wait_for(f"{cdp}/json/version", "Electron", windows[0])
        print(f"[OK] relaunched in {time.monotonic() - relaunch_started:.1f} s", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.electron_dev", description=__doc__.split("\n")[0])
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--data-dir", type=Path, help="library data dir for the backend (default: as webui-backend)")
    source.add_argument("--fixture", action="store_true", help="a throwaway two-track fixture library")
    parser.add_argument("--no-watch", action="store_true", help="do not rebuild and relaunch on shell edits")
    args = parser.parse_args(argv)

    from apps.webui.port_config import claim_ports

    ports = claim_ports()
    backend_origin = f"http://127.0.0.1:{ports.backend}"
    vite_origin = f"http://127.0.0.1:{ports.frontend}"
    pnpm = _resolve("pnpm")
    started: list[tuple[subprocess.Popen[bytes], str]] = []
    # Every child this run starts, service or setup step, gets the claimed pair.
    claimed = with_ports(dict(os.environ), backend=ports.backend, frontend=ports.frontend)

    # Refuse before building the fixture: the builder reseeds its library, and
    # a backend already up may be serving that very library (R-5).
    backend_up = _answers(f"{backend_origin}/api/v1/health")
    refusal = backend_reuse_refusal(
        backend_up=backend_up, explicit_source=args.fixture or args.data_dir is not None, origin=backend_origin
    )
    if refusal is not None:
        raise SystemExit(refusal)

    backend_env = _backend_env(args, backend_port=ports.backend, frontend_port=ports.frontend, backend_up=backend_up)

    # One slot, so the cleanup below stops whichever window a relaunch left running.
    windows: list[subprocess.Popen[bytes]] = []
    try:
        if backend_up:
            _refuse_unless_ours("backend", backend_origin, _expected_state_db(), ports.backend)
            print(f"[OK] reusing the backend already on {backend_origin}", flush=True)
        else:
            backend = _spawn(
                [sys.executable, "-m", "apps.webui.server", "--port", str(ports.backend), "--reload"],
                cwd=REPO_ROOT,
                env=backend_env,
                service="backend",
                register=lambda process: started.append((process, "backend")),
            )
            _wait_for(f"{backend_origin}/api/v1/health", "backend", backend)
            print(f"[OK] backend (reloading) on {backend_origin}", flush=True)

        if _answers(vite_origin):
            # Through Vite's /api proxy: it must reach the backend this run uses.
            _refuse_unless_ours("Vite server", vite_origin, _backend_state_db(backend_origin), ports.frontend)
            print(f"[OK] reusing the Vite server already on {vite_origin}", flush=True)
        else:
            vite = _spawn(
                [pnpm, "exec", "vite", "dev", "--host", "127.0.0.1"],
                cwd=FRONTEND_DIR,
                env=claimed,
                service="vite",
                register=lambda process: started.append((process, "vite")),
            )
            _wait_for(vite_origin, "vite", vite)
            print(f"[OK] Vite (HMR) on {vite_origin}", flush=True)

        build = shell_build_argv(pnpm)
        stamp = shell_sources_stamp(ELECTRON_DIR / "src")
        if not _build_shell(build, ELECTRON_DIR, claimed):
            return 1
        from apps.shared.user_identity import running_as_root

        is_root = running_as_root()
        plan = plan_launch(
            electron_binary=_electron_binary(),
            backend_port=ports.backend,
            frontend_port=ports.frontend,
            environ=dict(os.environ),
            platform=sys.platform,
            is_root=is_root,
            xvfb_run=_find("xvfb-run"),  # plan_launch owns the missing-xvfb error
        )
        if not _port_free(plan.cdp_port):
            raise SystemExit(
                f"[ERROR] CDP port {plan.cdp_port} is taken: is another electron-dev running in this worktree?"
            )
        plan.state_dir.mkdir(parents=True, exist_ok=True)
        window = _spawn(plan.argv, env=plan.env, service="electron", register=windows.append)
        cdp = f"http://127.0.0.1:{plan.cdp_port}"
        _wait_for(f"{cdp}/json/version", "Electron", window)
        print(
            "\n[OK] Electron is up. Edit the web UI and it hot-reloads; edit apps/desktop/electron/src and it relaunches.\n"
            f"  window origin  {vite_origin}  (backend {backend_origin})\n"
            f"  CDP            {cdp}\n"
            f"  profile        {plan.state_dir}\n"
            "  drive it       claude mcp add opendj-electron "
            f"-e MDT_WEBVIEW_MCP_CDP={cdp} -e MDT_WEBVIEW_MCP_APP={vite_origin} -e MDT_WEBVIEW_MCP_ENGINE={backend_origin} "
            "-- pnpm --dir apps/desktop exec tsx mcp/webview-mcp.ts\n"
            f"  or             chrome-devtools-mcp --browser-url={cdp}\n",
            flush=True,
        )

        return _watch_shell(
            windows,
            plan,
            cdp=cdp,
            shell=ShellBuild(build, ELECTRON_DIR, stamp, claimed),
            no_watch=args.no_watch,
            started=started,
            # Only a backend this run started: a reused one is not ours to stop.
            backend_health=None if backend_up else f"{backend_origin}/api/v1/health",
        )
    except KeyboardInterrupt:
        return 0
    finally:
        # A second Ctrl+C, or `just` forwarding the same SIGTERM, must not cut
        # the cleanup short and orphan Vite or the backend (seen Wed 7 Oct 2026).
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, signal.SIG_IGN)
        for window in windows:
            _stop(window, "Electron")
        for process, what in reversed(started):
            _stop(process, what)


class Terminated(BaseException):
    """SIGTERM, raised where the run is so its cleanup still runs.

    Not a KeyboardInterrupt: Ctrl+C is the user ending a dev session and exits 0,
    while SIGTERM comes from a CI timeout or a service manager, and must not read
    as a successful run.
    """


def _terminate(*_: object) -> None:
    raise Terminated


def run(body: Callable[[], int]) -> int:
    """Run ``body`` with SIGTERM as a stop that cleans up and exits 143 (128 + SIGTERM)."""
    signal.signal(signal.SIGTERM, _terminate)
    try:
        status = body()
    except Terminated:
        status = 128 + signal.SIGTERM
    return exit_status(status, _CLEANUP_FAILURES)


if __name__ == "__main__":
    raise SystemExit(run(main))
