"""Every self-hosted node job runs the node preflight before `pnpm install`.

Regression lines:
  - if a job installs with pnpm without the preflight then a missing tool or
    a full disk fails mid-install with a cryptic pnpm error
  - if the preflight runs after the install then it guards nothing
  - if the script stops failing on a full disk then the tripwire is gone
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
SCRIPT = REPO_ROOT / "scripts" / "ci_node_preflight.sh"
PREFLIGHT = "scripts/ci_node_preflight.sh apps/webui/frontend"

#: Linux jobs that run `pnpm install`; macOS packaging is hosted and has no
#: shared store, and GNU df is not there either.
NODE_JOBS = (("ci.yml", "frontend"), ("ci.yml", "quality"), ("e2e.yml", None))


def _steps(workflow: str, job_id: str | None) -> list[tuple[str, list[dict]]]:
    doc = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))
    jobs = doc["jobs"]
    if job_id is not None:
        return [(job_id, jobs[job_id]["steps"])]
    return [
        (jid, job["steps"])
        for jid, job in jobs.items()
        if any("pnpm install" in (s.get("run") or "") for s in job.get("steps") or [])
    ]


def test_every_pnpm_install_is_preceded_by_the_node_preflight() -> None:
    """if the preflight is missing or late then the store is unguarded"""
    for workflow, job_id in NODE_JOBS:
        for jid, steps in _steps(workflow, job_id):
            runs = [s.get("run") or "" for s in steps]
            installs = [i for i, r in enumerate(runs) if "pnpm install" in r]
            preflights = [i for i, r in enumerate(runs) if PREFLIGHT in r]
            assert installs, f"{workflow}:{jid} has no pnpm install; drop it from NODE_JOBS"
            assert preflights, f"{workflow}:{jid} installs without {PREFLIGHT}"
            assert min(preflights) < min(installs), f"{workflow}:{jid}: preflight after install"


def test_preflight_script_is_executable_and_fails_closed_on_a_full_disk(tmp_path: Path) -> None:
    """if the disk floor stops failing then a full runner reds mid-install again"""
    assert SCRIPT.stat().st_mode & stat.S_IXUSR, "scripts/ci_node_preflight.sh is not executable"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name in ("corepack", "node"):
        (fake_bin / name).write_text("#!/usr/bin/env bash\nexit 0\n")
        (fake_bin / name).chmod(0o755)
    env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}", "RUNNER_NAME": "probe-runner"}

    # An impossible floor stands in for a full disk: the check has to fail.
    result = subprocess.run(
        [str(SCRIPT), str(tmp_path)],
        env={**env, "MDT_CI_MIN_FREE_GB": "999999"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "probe-runner has" in result.stderr and "below the 999999G floor" in result.stderr

    # CONTROL: a zero floor passes with the same harness and names the runner.
    result = subprocess.run(
        [str(SCRIPT), str(tmp_path)],
        env={**env, "MDT_CI_MIN_FREE_GB": "0"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "probe-runner: corepack and node present" in result.stdout
