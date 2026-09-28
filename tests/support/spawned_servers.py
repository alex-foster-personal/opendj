"""Spawn a test server that is killed with its whole process group, always.

Registered as a pytest plugin from the root ``conftest.py``. Use the
``spawned_server`` fixture (a factory) or call ``spawn_test_server`` directly
and ``stop()`` the handle in your own finalizer.

Every server runs under ``scripts/server_owner_guard.py`` in a NEW session, so:

  - normal teardown and a FAILING test both run the fixture finalizer, which
    signals the server's whole group (TERM, then KILL) and fails loud if any
    member survives;
  - a Ctrl-C runs the same finalizers, and ``pytest_sessionfinish`` stops any
    server a test forgot to stop;
  - a SIGTERM/SIGKILL of pytest itself skips all Python cleanup, and that case
    is the guard's: it watches this pytest process and kills the group when
    the process disappears;
  - the server carries ``AF_SERVICE_ID=<namespace>.test.<name>``, so the fleet
    census (``scripts/orphan_reaper.py``) attributes anything that still leaks.

Regression lines:
  - if a spawned server is alive after its fixture tears down then broken
  - if a spawned server is alive after a FAILING test's teardown then broken
  - if a spawned server is alive 5 s after pytest is SIGKILLed then broken
  - if a spawned server's env lacks AF_SERVICE_ID=<namespace>.test.<name> then broken
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GUARD = REPO_ROOT / "scripts" / "server_owner_guard.py"
TERM_GRACE_S = 10.0
KILL_GRACE_S = 5.0

_LIVE: dict[int, SpawnedServer] = {}


@dataclass
class SpawnedServer:
    name: str
    proc: subprocess.Popen[Any]
    pgid: int

    @property
    def pid(self) -> int:
        """The GUARD's pid, which is also the group id; the server is its child."""
        return self.proc.pid

    def stop(self) -> None:
        """Kill the whole group; raise if anything in it is still running."""
        # Lazy on purpose: the root conftest loads this plugin in EVERY pytest
        # run, including pytest-only toolchains without psutil (ci.yml quality
        # job). Only a run that actually spawned a server reaches this line.
        from apps.shared.process_groups import (
            group_has_live_member,
            signal_group,
            wait_group_gone,
        )

        _LIVE.pop(self.pgid, None)
        for sig, grace in ((signal.SIGTERM, TERM_GRACE_S), (signal.SIGKILL, KILL_GRACE_S)):
            if signal_group(self.pgid, sig) is not None:
                break  # already gone, or only zombies left (macOS answers EPERM then)
            if wait_group_gone(self.pgid, grace):
                break
        if self.proc.poll() is None:
            self.proc.wait(timeout=KILL_GRACE_S)
        if group_has_live_member(self.pgid):
            raise RuntimeError(f"test server {self.name!r} (group {self.pgid}) survived SIGKILL")


def spawn_test_server(
    name: str,
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    stdout: IO[Any] | int | None = None,
    stderr: IO[Any] | int | None = None,
) -> SpawnedServer:
    """Start ``argv`` under the guard, owned by THIS pytest process."""
    guarded = [
        sys.executable,
        str(GUARD),
        "--owner-pid",
        str(os.getpid()),
        "--name",
        name,
        "--",
        *argv,
    ]
    proc = subprocess.Popen(
        guarded,
        cwd=cwd,
        env=env,
        stdout=stdout,
        stderr=stderr,
        start_new_session=True,
    )
    server = SpawnedServer(name=name, proc=proc, pgid=proc.pid)
    _LIVE[server.pgid] = server
    return server


@pytest.fixture
def spawned_server() -> Iterator[Callable[..., SpawnedServer]]:
    """Factory fixture: ``spawned_server("engine", argv, cwd=..., env=...)``."""
    mine: list[SpawnedServer] = []

    def _spawn(name: str, argv: list[str], **kwargs: Any) -> SpawnedServer:
        server = spawn_test_server(name, argv, **kwargs)
        mine.append(server)
        return server

    yield _spawn
    for server in reversed(mine):
        server.stop()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Stop every server a test spawned directly and never stopped."""
    leaked = list(_LIVE.values())
    for server in leaked:
        server.stop()
    if leaked:
        names = ", ".join(f"{s.name}(group {s.pgid})" for s in leaked)
        print(
            f"\n[spawned_servers] stopped {len(leaked)} server(s) a test never stopped: {names}",
            file=sys.stderr,
        )


@pytest.fixture
def cleanup_pids() -> Iterator[list[int]]:
    """Belt and braces: whatever a test leaves alive by FAILING is killed here."""
    pids: list[int] = []
    yield pids
    for pid in pids:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
