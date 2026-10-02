"""The server owner guard's owner-death deadline and spawn window (DEVOPS-17).

[if] a guarded server outlives a dead owner by 5 s or loses its shutdown [then] fail, [else stop].

Real processes only: a real owner is SIGKILLed and real servers are timed.

Regression lines:
  - if a server that ignores SIGTERM is alive 5.0 s after its owner died then broken
  - if a server that handles SIGTERM gets no grace to finish shutting down then broken
  - if a shell wrapper's exit on SIGTERM gets the server below it killed mid-shutdown,
    or the guard lingers after its whole group has exited then broken
  - if a signal landing just after the guard spawns its child leaks the child then broken
"""

from __future__ import annotations

import contextlib
import os
import shlex
import signal
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.support.process_probes import GONE_WITHIN_S, alive, server_argv, wait_for

pytestmark = pytest.mark.requirement("DEVOPS-17")

REPO_ROOT = Path(__file__).resolve().parents[2]


# A server that reports its pid once its SIGTERM behavior is installed, so the
# owner is only killed after the behavior under test is live.
TERM_IGNORING_CODE = textwrap.dedent(
    """
    import os, signal, sys, time
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    open(sys.argv[1], "w").write(str(os.getpid()))
    time.sleep(600)
    """
)
# Needs COOPERATIVE_CLEANUP_S after SIGTERM to shut down, then records that it did.
COOPERATIVE_CLEANUP_S = 1.0
COOPERATIVE_CODE = textwrap.dedent(
    f"""
    import os, signal, sys, time
    def _shutdown(_signum, _frame):
        time.sleep({COOPERATIVE_CLEANUP_S})
        open(sys.argv[1] + ".graceful", "w").write("done")
        os._exit(0)
    signal.signal(signal.SIGTERM, _shutdown)
    open(sys.argv[1], "w").write(str(os.getpid()))
    time.sleep(600)
    """
)


@dataclass
class OwnerDeath:
    server_gone_after_s: float
    guard_gone_after_s: float


def _time_owner_death(
    server_code: str, pid_file: Path, cleanup_pids: list[int], *, shell_wrapper: bool = False
) -> OwnerDeath:
    """Guard ``server_code`` for a real owner, SIGKILL the owner, and time how
    long the server and the guard outlive it. The clock starts BEFORE the kill,
    so each figure can only overstate the real interval. ``shell_wrapper`` runs
    the server under ``/bin/sh -c`` (as every Playwright webServer is), so the
    guard's direct child is the shell, not the server."""
    server_argv = [sys.executable, "-c", server_code, str(pid_file)]
    if shell_wrapper:
        server_argv = ["/bin/sh", "-c", f"{shlex.join(server_argv)} & wait"]
    owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    cleanup_pids.append(owner.pid)
    guard = subprocess.Popen(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "server_owner_guard.py"),
            "--owner-pid",
            str(owner.pid),
            "--name",
            "deadline",
            "--",
            *server_argv,
        ],
        start_new_session=True,
    )
    cleanup_pids.append(guard.pid)
    wait_for(lambda: pid_file.exists() and pid_file.read_text() != "", 30, "server to start")
    server = int(pid_file.read_text())
    cleanup_pids.append(server)
    assert alive(server), "positive control: the server must be running before the kill"
    killed_at = time.monotonic()
    owner.kill()
    owner.wait(timeout=10)
    while alive(server):
        assert time.monotonic() - killed_at < 30, f"server {server} never died"
        time.sleep(0.02)
    server_gone_after = time.monotonic() - killed_at
    guard.wait(timeout=30)
    return OwnerDeath(server_gone_after, time.monotonic() - killed_at)


def test_a_server_that_ignores_sigterm_is_gone_within_the_bound_of_owner_death(
    tmp_path: Path, cleanup_pids: list[int]
) -> None:
    gone_after = _time_owner_death(
        TERM_IGNORING_CODE, tmp_path / "server.pid", cleanup_pids
    ).server_gone_after_s
    assert gone_after <= GONE_WITHIN_S, (
        f"a TERM-ignoring server outlived its owner by {gone_after:.2f} s (bound {GONE_WITHIN_S} s)"
    )


# Poll interval plus a slow ps probe: well under the 4.5 s the guard would
# linger if it waited out its deadline instead of watching its group empty.
GUARD_EXIT_SLACK_S = 1.5


@pytest.mark.parametrize("shell_wrapper", [False, True], ids=["direct-child", "under-sh-c"])
def test_a_server_that_handles_sigterm_gets_to_finish_its_shutdown(
    tmp_path: Path, cleanup_pids: list[int], shell_wrapper: bool
) -> None:
    """The control for the bound above: escalating to SIGKILL early (or only)
    would satisfy that test and break every server's graceful shutdown. Under
    ``sh -c`` the shell dies on TERM at once, so the guard must wait for its
    GROUP, not its child; and once the group is empty it must exit promptly
    rather than sit out its deadline."""
    pid_file = tmp_path / "server.pid"
    timing = _time_owner_death(
        COOPERATIVE_CODE, pid_file, cleanup_pids, shell_wrapper=shell_wrapper
    )
    graceful = pid_file.with_name(pid_file.name + ".graceful")
    assert graceful.exists(), "the server was killed before its SIGTERM shutdown could finish"
    assert COOPERATIVE_CLEANUP_S <= timing.server_gone_after_s <= GONE_WITHIN_S, timing
    assert timing.guard_gone_after_s <= timing.server_gone_after_s + GUARD_EXIT_SLACK_S, timing


# Fault injection, real signal: a sitecustomize that makes the guard SIGTERM
# ITSELF the instant Popen returns for the marked child, which is the window a
# Playwright teardown can land in. Only the timing is forced; nothing is mocked.
TERM_AFTER_SPAWN_MARKER = "term-after-spawn-control"
TERM_AFTER_SPAWN_SITECUSTOMIZE = textwrap.dedent(
    f"""
    import os, signal, subprocess
    _real_init = subprocess.Popen.__init__
    def _init(self, args, *rest, **kwargs):
        _real_init(self, args, *rest, **kwargs)
        if {TERM_AFTER_SPAWN_MARKER!r} in " ".join(map(str, args)):
            os.kill(os.getpid(), signal.SIGTERM)
    subprocess.Popen.__init__ = _init
    """
)


def _live_members_of_group(pgid: int) -> list[int]:
    out = subprocess.run(
        ["ps", "-A", "-o", "pid=,pgid=,stat="],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "LC_ALL": "C"},
    ).stdout
    rows = [line.split() for line in out.splitlines()]
    return [
        int(r[0]) for r in rows if len(r) == 3 and int(r[1]) == pgid and not r[2].startswith("Z")
    ]


def test_a_signal_just_after_the_spawn_does_not_leak_the_child(tmp_path: Path) -> None:
    inject = tmp_path / "inject"
    inject.mkdir()
    (inject / "sitecustomize.py").write_text(TERM_AFTER_SPAWN_SITECUSTOMIZE)
    guard = subprocess.Popen(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "server_owner_guard.py"),
            "--owner-pid",
            str(os.getpid()),
            "--name",
            "spawnwindow",
            "--",
            *server_argv(tmp_path / "server.json", TERM_AFTER_SPAWN_MARKER),
        ],
        env={**os.environ, "PYTHONPATH": str(inject)},
        start_new_session=True,
    )
    try:
        guard.wait(timeout=30)
        wait_for(
            lambda: not _live_members_of_group(guard.pid),
            GONE_WITHIN_S,
            f"group {guard.pid} to be empty after the guard was signalled",
        )
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(guard.pid, signal.SIGKILL)
