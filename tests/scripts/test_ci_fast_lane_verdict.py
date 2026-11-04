"""PR CI runs no Trunk job, and a shard's verdict is its own exit code.

ADR-NEW-trunk-removed-from-pr-ci (the maintainer, Wed 4 Nov 2026: "chuck out trunk
altogether"). Until then ci.yml ran `trunk-quarantine-list` and
`trunk-quarantine` on every pull request, the pytest shards and the contracts
job waited on them for a list that excused failures, and five `trunk-*.yml`
workflows watched the Trunk merge queue nobody used since Fri 30 Oct 2026.

  - [if] a shard or cargo suite exits non-zero [then] its job is red, [else stop].

Regression lines:
  - if a ci.yml job id starts with `trunk` then a PR pays for Trunk again
  - if `test` or `contracts` needs anything but `scope` then a dead job sits in
    front of the shards on the queue-bound light pool
  - if the verdict step reads a quarantine list then a failure can be excused
  - if the verdict step loses always() or gains continue-on-error then a red
    shard reads green
  - if a `trunk-*.yml` workflow returns then the merge-queue watchers are back
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
CI = WORKFLOWS / "ci.yml"
VERDICT_STEP = "Fast lane verdict (pytest exit code)"
CARGO_VERDICT_STEP = "Cargo verdict (nextest exit code)"

pytestmark = pytest.mark.requirement("DEVOPS-8500")


def _jobs() -> dict:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]


def _step(job: str, name: str) -> dict:
    steps = [s for s in _jobs()[job]["steps"] if s.get("name") == name]
    assert len(steps) == 1, f"{job}: want one step named {name!r}, got {len(steps)}"
    return steps[0]


def test_no_ci_job_is_a_trunk_job() -> None:
    jobs = _jobs()
    assert not [job for job in jobs if job.startswith("trunk")], sorted(jobs)
    for job_id, job in jobs.items():
        needs = job.get("needs", [])
        needs = [needs] if isinstance(needs, str) else needs
        assert not [n for n in needs if n.startswith("trunk")], (job_id, needs)


def test_no_trunk_workflow_file_exists() -> None:
    assert not sorted(WORKFLOWS.glob("trunk-*.yml")), sorted(WORKFLOWS.glob("trunk-*.yml"))
    # Positive control: the glob finds a workflow that does exist.
    assert sorted(WORKFLOWS.glob("ci.yml")) == [CI]


@pytest.mark.parametrize("job_id", ["test", "contracts"])
def test_the_test_and_contracts_jobs_wait_on_scope_alone(job_id: str) -> None:
    assert _jobs()[job_id]["needs"] == ["scope"], _jobs()[job_id]["needs"]


def test_the_verdict_step_is_last_unconditional_and_reads_no_list() -> None:
    steps = _jobs()["test"]["steps"]
    verdict = steps[-1]
    assert verdict["name"] == VERDICT_STEP, "the verdict must be the job's last step"
    assert verdict["if"] == "always()"
    assert "continue-on-error" not in verdict
    assert verdict["env"]["PYTEST_OUTCOME"] == "${{ steps.pytest-shard.outcome }}"
    assert verdict["env"]["PYTEST_RC"] == "${{ steps.pytest-shard.outputs.rc }}"
    assert not [k for k in verdict["env"] if "TRUNK" in k], verdict["env"]
    assert "quarantine" not in verdict["run"].lower()


def test_the_cargo_verdict_reads_no_list() -> None:
    verdict = _step("contracts", CARGO_VERDICT_STEP)
    assert verdict["if"] == "always()"
    assert "continue-on-error" not in verdict
    assert not [k for k in verdict["env"] if "TRUNK" in k], verdict["env"]
    assert "quarantine" not in verdict["run"].lower()


def _run_verdict_step(outcome: str, rc: str) -> int:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("UNAVAILABLE: no bash on this host, so the step's own run block cannot be executed")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTEST_OUTCOME": outcome,
        "PYTEST_RC": rc,
        "SHARD": "1",
    }
    return subprocess.run(
        [bash, "-e", "-c", _step("test", VERDICT_STEP)["run"]],
        env=env,
        capture_output=True,
        timeout=30,
        check=False,
    ).returncode


@pytest.mark.parametrize(
    ("outcome", "rc", "want"),
    [
        ("success", "0", 0),
        ("failure", "1", 1),
        ("failure", "124", 1),
        ("failure", "", 1),
        ("skipped", "", 1),
        ("cancelled", "", 1),
    ],
)
def test_the_verdict_step_executes_as_written(outcome: str, rc: str, want: int) -> None:
    assert _run_verdict_step(outcome, rc) == want


def test_the_cargo_verdict_gates_the_live_core_suite() -> None:
    """The live-core cargo suite is a required input to the verdict, gated like desktop and audio."""
    step = _step("contracts", "Run live core Rust tests")
    assert step["id"] == "live-rust"
    assert "apps/live-core/Cargo.toml" in step["run"]
    assert step["run"].index('rm -f "$junit_src"') < step["run"].index("cargo nextest run")
    assert "nextest JUnit missing (live)" in step["run"]
    verdict = _step("contracts", CARGO_VERDICT_STEP)
    assert verdict["env"]["LIVE_OUTCOME"] == "${{ steps.live-rust.outcome }}"
    assert verdict["env"]["LIVE_RC"] == "${{ steps.live-rust.outputs.rc }}"
    assert "decide rust-live" in verdict["run"]
    assert "junit-rust-live.xml" in verdict["run"]
    # Positive control: the same lookup finds the audio suite's verdict wiring.
    assert "decide rust-audio" in verdict["run"]
