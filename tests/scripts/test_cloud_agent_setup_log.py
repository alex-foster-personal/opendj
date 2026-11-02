"""scripts/cloud_agent_setup.sh keeps its run log even when it fails fast.

The script copies its own output into ~/.cloud-env/setup.log through a
background ``tee``. A failure in the first lines (here: no pnpm pin to read)
exits within milliseconds, and before the EXIT trap waited on tee, bash could
exit before tee had written anything (a timing race, not forced here), so the
run-start line that
(private path) looks for was missing on exactly the runs a later
session most needs to see.

Regression lines:
  - if a fast _die leaves ~/.cloud-env/setup.log without its run-start line then broken
  - if that run's error line is missing from the log then broken
  - if the EXIT trap waits on tee without closing its pipe first then broken (the run hangs)
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "cloud_agent_setup.sh"
RUNS = 3

BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="cloud_agent_setup.sh is a bash script; no bash on PATH")


def test_fast_failure_still_logs_its_run(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    (tree / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPT, tree / "scripts" / SCRIPT.name)
    # The script sources its helpers before it opens the log.
    shutil.copytree(REPO_ROOT / "scripts" / "lib", tree / "scripts" / "lib")
    # A package.json with no packageManager pin: the script's _die fires on
    # the first check after the log starts.
    (tree / "apps" / "webui" / "frontend").mkdir(parents=True)
    (tree / "apps" / "webui" / "frontend" / "package.json").write_text("{}\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    # The real tee, unmodified: no PATH shim or other stand-in (AGENTS.md
    # forbids test doubles). The race this guards is timing-dependent, so a
    # pass here does not prove the fix; the mutation check in the PR does.
    env = {**os.environ, "HOME": str(home)}
    env.pop("DOPPLER_TOKEN", None)

    log_path = home / ".cloud-env" / "setup.log"
    for run in range(1, RUNS + 1):
        # Wait for bash alone, never for EOF on its output: a pipe the caller
        # reads to EOF would also wait out tee and hide the race. The log is
        # read the moment bash has exited, as a runner that tears the
        # container down after setup would leave it.
        returncode = subprocess.run(
            [str(BASH), str(tree / "scripts" / SCRIPT.name), "--check"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            check=False,
            timeout=30,
        ).returncode
        assert returncode == 1, f"run {run}: expected the pnpm-pin _die (exit 1), got {returncode}"
        log = log_path.read_text(encoding="utf-8")
        assert log.count("[cloud-env] run start ") == run, f"run {run}: run-start line missing\n{log}"
        assert log.count("[cloud-env][ERROR] could not read the pnpm pin") == run, (
            f"run {run}: error line missing\n{log}"
        )
