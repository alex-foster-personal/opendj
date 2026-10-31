"""Run the Cmd K pick-race browser gate on a CI runner (CMDK-16).

The step used to be shell: a host lock, a port reaper, and a bare ``pnpm`` name. This
helper resolves every program it launches by path, passes arguments as lists, and
fails with the reason, so the step means the same on any platform the runner has. The
wrappers it calls (the host lock and the port reaper) stay where they are.

Usage (from the workflow, with the repository as the working directory)::

    python3 scripts/ci_cmdk_pick_race_gate.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT: Path = Path(__file__).resolve().parents[1]
FRONTEND_ROOT: Path = REPOSITORY_ROOT / "apps" / "webui" / "frontend"
PICK_RACE_PORTS: tuple[str, ...] = ("5336", "8716")
PICK_RACE_CONFIG: str = "tests/e2e/playwright.cmdk-pick-race.config.ts"
PICK_RACE_OUTPUT: str = "test-results/cmdk-pick-race"


def resolve(name: str) -> str:
    """The absolute path of a program on PATH, or SystemExit naming it."""
    found = shutil.which(name)
    if found is None:
        raise SystemExit(f"[ERROR] {name} is not on PATH; the pick-race gate cannot run without it")
    return found


def run(argv: list[str], cwd: Path, env: dict[str, str]) -> None:
    result = subprocess.run(argv, cwd=cwd, env=env, check=False)
    if result.returncode != 0:
        raise SystemExit(f"[ERROR] {argv[0]} exited {result.returncode}")


def bash_run(script: Path, *args: str) -> list[str]:
    """A wrapper script launched through the bash found on PATH, not by its own mode or shebang.

    The wrappers are bash, so bash is the one shell this gate names. A runner without it fails here,
    with the reason, rather than with a bare "not executable" from the launch.
    """
    return [resolve("bash"), str(script), *args]


def pick_race_argv(pnpm: str) -> list[str]:
    """The host-locked, port-reaped Playwright run, with every program already resolved."""
    lock = REPOSITORY_ROOT / "scripts" / "ci_host_lock.sh"
    reaper = REPOSITORY_ROOT / "scripts" / "ci_reap_port_holders.sh"
    return bash_run(
        lock, "cmdk-pick-race",
        str(reaper), *PICK_RACE_PORTS, "--",
        pnpm, "exec", "playwright", "test", "--config", PICK_RACE_CONFIG, "--output", PICK_RACE_OUTPUT,
    )


def main() -> int:
    run(pick_race_argv(resolve("pnpm")), FRONTEND_ROOT, dict(os.environ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
