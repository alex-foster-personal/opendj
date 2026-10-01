"""The e2e output-topology step that lands on main ahead of the preview (PR #3837).

It names a playwright config only the preview holds. Both directions are pinned: a tree
WITHOUT the config skips and says so, and a tree WITH it runs the gate and keeps its exit
code. An overshoot that skipped whenever anything went wrong would turn a red gate into a
silent pass.

Regression lines:
  - if the output-topology step fails on a tree with no config then main's e2e is red
  - if the output-topology step passes when playwright fails then the gate is decoration
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
E2E = REPO_ROOT / ".github" / "workflows" / "e2e.yml"
STEP = "Hermetic gate - master12-cue34 output topology"
CONFIG = "tests/e2e/playwright.audio-output-topology.config.ts"


def _step_script() -> str:
    jobs = yaml.safe_load(E2E.read_text())["jobs"]
    found = [s for job in jobs.values() for s in job.get("steps", []) if s.get("name") == STEP]
    assert len(found) == 1, f"expected exactly one {STEP!r} step, found {len(found)}"
    return found[0]["run"]


def _run_step(frontend: Path, pnpm_exit: int) -> subprocess.CompletedProcess[str]:
    """The step's own script under bash -e, as Actions runs it, with `pnpm` answering `pnpm_exit`."""
    bin_dir = frontend / "bin"
    bin_dir.mkdir()
    pnpm = bin_dir / "pnpm"
    pnpm.write_text(f'#!/bin/sh\necho "pnpm $*"\nexit {pnpm_exit}\n')
    pnpm.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    return subprocess.run(
        ["bash", "-e", "-c", _step_script()], cwd=frontend, env=env, capture_output=True, text=True, check=False
    )


def test_the_topology_step_skips_and_says_so_without_its_config(tmp_path: Path) -> None:
    done = _run_step(tmp_path, pnpm_exit=1)
    assert done.returncode == 0, done.stderr
    assert f"SKIPPED: {CONFIG} is not in this tree" in done.stdout
    assert "pnpm" not in done.stdout.replace(CONFIG, "")


@pytest.mark.parametrize("pnpm_exit", [0, 1])
def test_the_topology_step_runs_the_gate_and_keeps_its_exit_code_with_its_config(
    tmp_path: Path, pnpm_exit: int
) -> None:
    config = tmp_path / CONFIG
    config.parent.mkdir(parents=True)
    config.write_text("export default {};\n")
    done = _run_step(tmp_path, pnpm_exit)
    assert done.returncode == pnpm_exit, done.stdout + done.stderr
    assert f"pnpm exec playwright test --config {CONFIG}" in done.stdout
    assert "SKIPPED" not in done.stdout
