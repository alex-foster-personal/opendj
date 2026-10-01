"""The e2e output-topology gate that lands on main ahead of its feature (PR #3837).

The feature is three files: the topology source, its playwright spec and that spec's
config. The gate may skip only when the FEATURE is absent, which means all three. Any
one present without the others fails, so deleting or renaming the config or the spec
cannot quietly switch the gate off, and with all three it runs and keeps the exit code.

Regression lines:
  - if the gate fails on a tree with none of the feature then main's e2e is red
  - if the gate skips while any feature file exists then a rename disables it for good
  - if the gate passes when playwright fails then the gate is decoration
  - if the e2e step stops calling this script then none of the above is enforced
"""

from __future__ import annotations

import itertools
import os
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
E2E = REPO_ROOT / ".github" / "workflows" / "e2e.yml"
SCRIPT = REPO_ROOT / "scripts" / "ci_output_topology_gate.sh"
STEP = "Hermetic gate - master12-cue34 output topology"
FRONTEND = "apps/webui/frontend"
SOURCE = "src/lib/rb/audio-output-topology.ts"
CONFIG = "tests/e2e/playwright.audio-output-topology.config.ts"
SPEC = "tests/e2e/audio-output-topology.spec.ts"
FEATURE = (SOURCE, CONFIG, SPEC)
PARTIAL = [
    present
    for size in (1, 2)
    for present in itertools.combinations(FEATURE, size)
]


def _run_gate(frontend: Path, present: tuple[str, ...], pnpm_exit: int) -> subprocess.CompletedProcess[str]:
    """The real script against a tree holding exactly `present`, with `pnpm` answering `pnpm_exit`."""
    for relative in present:
        (frontend / relative).parent.mkdir(parents=True, exist_ok=True)
        (frontend / relative).write_text("export default {};\n")
    bin_dir = frontend / "bin"
    bin_dir.mkdir()
    pnpm = bin_dir / "pnpm"
    pnpm.write_text(f'#!/bin/sh\necho "pnpm-ran $*"\nexit {pnpm_exit}\n')
    pnpm.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    return subprocess.run(
        ["bash", str(SCRIPT), str(frontend)], env=env, capture_output=True, text=True, check=False
    )


def test_the_gate_skips_and_says_so_only_when_the_whole_feature_is_absent(tmp_path: Path) -> None:
    done = _run_gate(tmp_path, (), pnpm_exit=1)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "SKIPPED: the output-topology feature is not in this tree" in done.stdout
    assert "pnpm-ran" not in done.stdout


@pytest.mark.parametrize("present", PARTIAL, ids=lambda p: "+".join(Path(x).name for x in p))
def test_the_gate_fails_when_part_of_the_feature_is_missing(tmp_path: Path, present: tuple[str, ...]) -> None:
    """pnpm would answer 0 here, so a nonzero exit is the gate's own refusal."""
    done = _run_gate(tmp_path, present, pnpm_exit=0)
    assert done.returncode != 0, done.stdout
    assert "pnpm-ran" not in done.stdout and "SKIPPED" not in done.stdout
    for relative in FEATURE:
        assert (f"missing: {relative}" in done.stderr) == (relative not in present), done.stderr


@pytest.mark.parametrize("pnpm_exit", [0, 1])
def test_the_gate_runs_and_keeps_its_exit_code_with_the_whole_feature(tmp_path: Path, pnpm_exit: int) -> None:
    done = _run_gate(tmp_path, FEATURE, pnpm_exit)
    assert done.returncode == pnpm_exit, done.stdout + done.stderr
    assert f"pnpm-ran exec playwright test --config {CONFIG}" in done.stdout
    assert "SKIPPED" not in done.stdout


def test_the_e2e_step_is_this_script_and_nothing_softens_it() -> None:
    jobs = yaml.safe_load(E2E.read_text())["jobs"]
    found = [s for job in jobs.values() for s in job.get("steps", []) if s.get("name") == STEP]
    assert len(found) == 1, f"expected exactly one {STEP!r} step, found {len(found)}"
    step = found[0]
    assert step["run"].strip() == f"bash scripts/ci_output_topology_gate.sh {FRONTEND}"
    assert "continue-on-error" not in step and "working-directory" not in step
    assert step["if"] == "${{ !cancelled() }}"


def test_this_tree_holds_the_whole_feature_or_none_of_it() -> None:
    """The real tree is never in the state the gate refuses."""
    held = [relative for relative in FEATURE if (REPO_ROOT / FRONTEND / relative).is_file()]
    assert held in ([], list(FEATURE)), f"only part of the output-topology feature is here: {held}"
