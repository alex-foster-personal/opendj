#!/usr/bin/env python3
"""Run a test-spawned server so it cannot outlive the test run that owns it.

    python scripts/server_owner_guard.py --owner-pid <pid> --name <name> -- <argv...>

WHY. Measured Mon 28 Sep 2026 against Playwright 1.61: a ``webServer`` started
by ``playwright test`` SURVIVES when the runner receives SIGTERM or SIGKILL
(only SIGINT runs Playwright's cleanup). The server is reparented to
init/launchd and keeps its port. SIGTERM and SIGKILL are exactly what an
agent's command timeout, a cancelled CI step and an OOM kill deliver, so every
killed e2e run leaks its servers. pytest has the same gap for any server a test
spawns in its own session.

WHAT THIS DOES. The guard runs ``argv`` as its child IN THE GUARD'S OWN
PROCESS GROUP, with ``AF_SERVICE_ID=<namespace>.test.<name>`` (namespace from
``[tool.af] process_namespace`` in pyproject.toml), then waits. It kills its
whole process group -- itself, the child, and every descendant that did not
start its own session -- when ANY of these happens:

  - the OWNER pid dies, or is recycled (its start time changes);
  - the guard receives SIGTERM, SIGINT or SIGHUP;
  - the child exits (the guard exits with the child's status).

The group, not the child, is the unit on purpose: ``uv run`` and ``sh -c``
put the real server one or two levels below the child, and a group signal
reaches all of them at once. Staying in the CALLER'S group (never ``setsid``)
matters too: Playwright tears a webServer down by signalling that group, so
its normal teardown still reaches the guard and the server in one step.

Callers:
  - Playwright: ``guardedWebServerCommand`` in
    ``apps/webui/frontend/tests/e2e/support/guarded-web-server.ts``.
  - pytest: ``tests/support/spawned_servers.py`` (``spawn_test_server``).

Requirements (mini-PRD):
  ✔︎ the guarded server dies when the owner dies
    - [if] the owner is SIGKILLed and the server is still alive 5 s later [then ⛔️]
    - [if] the owner pid is recycled (start time differs) and the server survives [then ⛔️]
  ✔︎ the guarded server dies with its guard
    - [if] the guard gets SIGTERM and the server survives [then ⛔️]
  ✔︎ every guarded server is attributable
    - [if] the server's env lacks AF_SERVICE_ID=<namespace>.test.<name> [then ⛔️]
    - [if] pyproject has no [tool.af] process_namespace and the guard starts anyway [then ⛔️]
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
import tomllib
from pathlib import Path


# ---------------------------------------------------------------- config


class CFG:
    REPO_ROOT = Path(__file__).resolve().parents[1]
    POLL_S = 0.25
    TERM_GRACE_S = 5.0
    TEST_SERVICE_INFIX = ".test."


# ---------------------------------------------------------------- helpers


def process_namespace(repo_root: Path = CFG.REPO_ROOT) -> str:
    """``[tool.af] process_namespace`` from pyproject.toml; fail loud when absent."""
    pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    namespace = pyproject.get("tool", {}).get("af", {}).get("process_namespace")
    if not namespace:
        raise SystemExit("[ERROR] pyproject.toml has no [tool.af] process_namespace")
    return str(namespace)


def testing_service_id(name: str, repo_root: Path = CFG.REPO_ROOT) -> str:
    return f"{process_namespace(repo_root)}{CFG.TEST_SERVICE_INFIX}{name}"


def process_start_time(pid: int) -> str | None:
    """Identity of a pid: its start time. None when the pid is gone or a zombie."""
    if sys.platform.startswith("linux"):
        try:
            text = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return None
        fields = text[text.rfind(")") + 2 :].split()
        return None if fields[0] == "Z" else fields[19]
    out = subprocess.run(
        ["ps", "-o", "stat=,lstart=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "LC_ALL": "C"},
    ).stdout.strip()
    if not out or out.startswith("Z"):
        return None
    return out.partition(" ")[2].strip()


def _log(message: str) -> None:
    """Best-effort diagnostic. The guard's stderr is usually a pipe to the very
    owner whose death it is reporting, so the write can hit EPIPE; that must
    never stop the kill, which is why every caller signals FIRST and logs after.
    """
    try:
        print(f"[server-owner-guard] {message}", file=sys.stderr, flush=True)
    except BrokenPipeError:
        pass  # the reader is the dead owner; there is nobody left to tell


def _kill_own_group(reason: str) -> None:
    """TERM the guard's whole group, wait, then KILL it (the guard included)."""
    pgid = os.getpgrp()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)  # survive our own TERM long enough to escalate
    os.killpg(pgid, signal.SIGTERM)
    _log(f"{reason}; sent SIGTERM to group {pgid}")
    deadline = time.monotonic() + CFG.TERM_GRACE_S
    while time.monotonic() < deadline:
        try:
            if os.waitpid(-1, os.WNOHANG) == (0, 0):
                time.sleep(CFG.POLL_S)
        except ChildProcessError:
            break  # every child of ours is reaped; others in the group get the KILL below
    os.killpg(pgid, signal.SIGKILL)


# ---------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--owner-pid", type=int, required=True, help="the test runner that owns this server")
    parser.add_argument("--name", required=True, help="short server name, e.g. engine or vite")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- argv to run")
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("no command given after --")

    owner_identity = process_start_time(args.owner_pid)
    if owner_identity is None:
        raise SystemExit(f"[ERROR] owner pid {args.owner_pid} is not running: refusing to start an unowned server")
    env = {**os.environ, "AF_SERVICE_ID": testing_service_id(args.name), "OPENDJ_TEST_OWNER_PID": str(args.owner_pid)}
    child = subprocess.Popen(command, env=env)

    def _on_signal(signum: int, _frame: object) -> None:
        _kill_own_group(f"{args.name}: got signal {signum}")

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, _on_signal)

    while True:
        status = child.poll()
        if status is not None:
            return status
        if process_start_time(args.owner_pid) != owner_identity:
            _kill_own_group(f"{args.name}: owner pid {args.owner_pid} is gone")
            return 1
        time.sleep(CFG.POLL_S)


if __name__ == "__main__":
    sys.exit(main())
