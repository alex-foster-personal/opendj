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
  - the child exits: anything it backgrounded into the group is killed, the
    guard itself is spared, and the guard exits with the child's status.

The group, not the child, is the unit on purpose: ``uv run`` and ``sh -c``
put the real server one or two levels below the child, and a group signal
reaches all of them at once. The guard never calls ``setsid``: Playwright
tears a webServer down by SIGKILLing the group it created for it, so guard and
server must share that group. The guard must LEAD it, though, and refuses to
start otherwise, so a group signal can never reach a caller's own processes:
Playwright's detached webServer shell ``exec``s the guard, and pytest starts it
with ``start_new_session``.

Callers:
  - Playwright: ``guardedWebServerCommand`` in
    ``apps/webui/frontend/tests/e2e/support/guarded-web-server.ts``.
  - pytest: ``tests/support/spawned_servers.py`` (``spawn_test_server``).

Requirements (mini-PRD):
  ✔︎ the guarded server dies when the owner dies
    - [if] the owner is SIGKILLed and the server is still alive 5 s later [then ⛔️]
    - [if] the owner pid is recycled (start time differs) and the server survives [then ⛔️]
  ✔︎ the owner-death bound holds even for a server that ignores SIGTERM
    - [if] a child that ignores SIGTERM is alive 5 s after its owner died [then ⛔️]
    - [if] a child that handles SIGTERM gets no grace to finish shutting down [then ⛔️]
  ✔︎ the guarded server dies with its guard
    - [if] the guard gets SIGTERM and the server survives [then ⛔️]
  ✔︎ nothing the child backgrounded outlives the child
    - [if] the child backgrounds a server and exits, and that server survives [then ⛔️]
    - [if] the guard's exit status differs from the child's [then ⛔️]
    - [if] the guard starts while not leading its process group [then ⛔️]
  ✔︎ every guarded server is attributable
    - [if] the server's env lacks AF_SERVICE_ID=<namespace>.test.<name> [then ⛔️]
    - [if] pyproject has no [tool.af] process_namespace and the guard starts anyway [then ⛔️]
"""

from __future__ import annotations

import argparse
import contextlib
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
    # DEVOPS-17: a guarded server is gone within this long of its owner's death.
    OWNER_DEATH_BOUND_S = 5.0
    # Kept inside that bound for the final SIGKILL's delivery and a slow probe.
    DEADLINE_MARGIN_S = 0.5
    # TERM-to-KILL grace for what a finished child left behind (no owner died).
    LEFTOVER_TERM_GRACE_S = 5.0
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
    # The reader is the dead owner; there is nobody left to tell.
    with contextlib.suppress(BrokenPipeError):
        print(f"[server-owner-guard] {message}", file=sys.stderr, flush=True)


def _kill_own_group(reason: str, kill_by: float) -> None:
    """TERM the guard's whole group, then KILL it (the guard included) no later
    than ``kill_by``, an absolute ``time.monotonic()`` deadline: a member that
    ignores SIGTERM must not stretch the owner-death bound."""
    pgid = os.getpgrp()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)  # survive our own TERM long enough to escalate
    os.killpg(pgid, signal.SIGTERM)
    _log(f"{reason}; sent SIGTERM to group {pgid}")
    while (remaining := kill_by - time.monotonic()) > 0:
        try:
            if os.waitpid(-1, os.WNOHANG) == (0, 0):
                time.sleep(min(CFG.POLL_S, remaining))
        except ChildProcessError:
            break  # every child of ours is reaped; others in the group get the KILL below
    os.killpg(pgid, signal.SIGKILL)


def _live_group_members_but_me() -> list[int]:
    """Live (non-zombie) members of the guard's group, the guard excluded."""
    pgid, me = os.getpgrp(), os.getpid()
    out = subprocess.run(
        ["ps", "-A", "-o", "pid=,pgid=,stat="],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "LC_ALL": "C"},
        process_group=0,  # else the probe lists ITSELF as a leftover, forever
    ).stdout
    rows = [line.split() for line in out.splitlines()]
    return [
        int(pid)
        for pid, member_pgid, state in (r for r in rows if len(r) == 3)
        if int(member_pgid) == pgid and int(pid) != me and not state.startswith("Z")
    ]


def _kill_group_leftovers(reason: str) -> None:
    """The child exited, but what it backgrounded is still in the group: kill
    that, TERM then KILL, and spare the guard so it can return the child's status."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        leftovers = _live_group_members_but_me()
        if not leftovers:
            return
        for pid in leftovers:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, sig)
        _log(f"{reason}; sent {sig.name} to leftover(s) {leftovers}")
        deadline = time.monotonic() + CFG.LEFTOVER_TERM_GRACE_S
        while time.monotonic() < deadline and _live_group_members_but_me():
            time.sleep(CFG.POLL_S)
    survivors = _live_group_members_but_me()
    if survivors:
        raise SystemExit(f"[ERROR] {reason}; group members survived SIGKILL: {survivors}")


# ---------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--owner-pid", type=int, required=True, help="the test runner that owns this server"
    )
    parser.add_argument("--name", required=True, help="short server name, e.g. engine or vite")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- argv to run")
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("no command given after --")

    if os.getpgrp() != os.getpid():
        raise SystemExit(
            f"[ERROR] the guard must lead its process group (pgid {os.getpgrp()} != pid "
            f"{os.getpid()}): exec it from the webServer shell, or start it with "
            "start_new_session, so its group signals reach only the server"
        )
    owner_seen_alive_at = time.monotonic()
    owner_identity = process_start_time(args.owner_pid)
    if owner_identity is None:
        raise SystemExit(
            f"[ERROR] owner pid {args.owner_pid} is not running: "
            "refusing to start an unowned server"
        )
    env = {
        **os.environ,
        "AF_SERVICE_ID": testing_service_id(args.name),
        "OPENDJ_TEST_OWNER_PID": str(args.owner_pid),
    }
    child = subprocess.Popen(command, env=env)

    def _on_signal(signum: int, _frame: object) -> None:
        kill_by = time.monotonic() + CFG.OWNER_DEATH_BOUND_S - CFG.DEADLINE_MARGIN_S
        _kill_own_group(f"{args.name}: got signal {signum}", kill_by)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, _on_signal)

    while True:
        status = child.poll()
        if status is not None:
            _kill_group_leftovers(f"{args.name}: child exited with status {status}")
            return status
        probe_started_at = time.monotonic()
        if process_start_time(args.owner_pid) != owner_identity:
            # The owner died after the last probe that saw it alive STARTED, so
            # this deadline falls inside the bound measured from its real death,
            # however late the poll noticed it.
            kill_by = owner_seen_alive_at + CFG.OWNER_DEATH_BOUND_S - CFG.DEADLINE_MARGIN_S
            _kill_own_group(f"{args.name}: owner pid {args.owner_pid} is gone", kill_by)
            return 1
        owner_seen_alive_at = probe_started_at
        time.sleep(CFG.POLL_S)


if __name__ == "__main__":
    sys.exit(main())
