"""Real-process probes shared by the orphan-cleanup test modules (DEVOPS-17).

Nothing here is a mock: ``alive`` asks the OS, ``orphan`` double-forks a real
process so it is reparented to init exactly like a leaked test server.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable

from scripts.server_owner_guard import process_start_time

GONE_WITHIN_S = 5.0


def alive(pid: int) -> bool:
    return process_start_time(pid) is not None


def wait_for(predicate: Callable[[], bool], timeout_s: float, what: str) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout_s}s waiting for {what}")
        time.sleep(0.1)


def orphan(argv: list[str], env: dict[str, str], cwd: str) -> int:
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
