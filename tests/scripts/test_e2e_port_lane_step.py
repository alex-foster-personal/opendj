"""Every e2e job must derive and assert MUSIC_DJ_PORT_LANE before server work.

`pnpm test:e2e` (apps.webui.port_config, via `apps/webui/frontend/playwright.config.ts`)
is the only step in this repo's CI that claims a dynamic pair from the shared
worktree-ports pool. Each self-hosted runner is a separate clone with its own
Git common dir, so its own registry and lock: two runners on one host never
contend for that lock, and both start allocating from slot 0 of the same
pool, so two concurrent CI jobs on one host could select the identical pair
(issue #1301: trunk red on agentbox-2 with every Playwright spec passing,
purely on "frontend port 9400 is already in use").

Regression lines:
  - if the lane-derivation step is missing, or runs after the claiming step,
    then MUSIC_DJ_PORT_LANE is unset when the pool is claimed and two runners
    on one host can collide again
  - if the derivation script stops failing on an unset RUNNER_NAME then a
    misconfigured runner silently claims lane 1, which a real runner "-0"
    could also resolve to, reopening the collision this guards against
  - if the derivation script ever maps two different runner names to the
    same lane then this suite would not know: that invariant is `port_config`'s
    job and is covered by tests/webui/test_worktree_port_config.py
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

import yaml

from tests.platform_capabilities import posix_bash_or_skip

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
LANE_STEP_NAME = "Derive the worktree-port lane from the runner identity"
LANE_ASSERTION_STEP_NAME = "Assert MUSIC_DJ_PORT_LANE is set"
STALE_SERVER_HYGIENE_STEP_NAME = "Reclaim stale E2E servers before port claim"
CLAIMING_RUN_SNIPPET = "test:e2e"
E2E_JOBS = ("gate", "extended")
# ci.yml's pytest jobs claim the same pool (engine, backend and frontend fixtures through
# apps/webui/port_config.py), so they carry the same two steps (Fri 30 Oct 2026: two shards on one
# host collided on lane 0, "address already in use", while the fleet right-sized its runner hosts).
CI_PYTEST_JOBS = ("test", "fast")


def _e2e_job_steps(job_name: str) -> list[dict]:
    doc = yaml.safe_load((WORKFLOWS / "e2e.yml").read_text(encoding="utf-8"))
    return doc["jobs"][job_name]["steps"]


def _ci_job_steps(job_name: str) -> list[dict]:
    doc = yaml.safe_load((WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))
    return doc["jobs"][job_name]["steps"]


def test_every_ci_pytest_job_derives_and_asserts_the_lane_before_pytest_runs() -> None:
    """if a pytest shard job loses the lane step, or runs it after pytest, then every shard
    on one host claims from slot 0 of the same pool window again"""
    e2e_script = next(step["run"] for step in _e2e_job_steps("gate") if step.get("name") == LANE_STEP_NAME)
    for job_name in CI_PYTEST_JOBS:
        steps = _ci_job_steps(job_name)
        names = [step.get("name") for step in steps]
        runs = [step.get("run") or "" for step in steps]
        assert LANE_STEP_NAME in names, f"ci.yml {job_name} is missing lane derivation"
        assert LANE_ASSERTION_STEP_NAME in names, f"ci.yml {job_name} is missing lane assertion"
        lane_index = names.index(LANE_STEP_NAME)
        assert lane_index < names.index(LANE_ASSERTION_STEP_NAME), job_name
        pytest_indices = [i for i, run in enumerate(runs) if "pytest" in run and i != lane_index]
        assert pytest_indices, f"ci.yml {job_name} no longer runs pytest; update this test"
        assert lane_index < min(pytest_indices), (
            f"ci.yml {job_name}: lane step at {lane_index}, first pytest step at {min(pytest_indices)}"
        )
        assert steps[lane_index]["run"] == e2e_script, (
            f"ci.yml {job_name}: lane derivation must be the same script as e2e.yml's"
        )


def test_lane_derivation_step_precedes_the_dynamic_pool_claim() -> None:
    """if the lane step is missing or late then the claim runs unlaned"""
    steps = _e2e_job_steps("gate")
    names = [step.get("name") for step in steps]
    runs = [step.get("run") or "" for step in steps]

    assert LANE_STEP_NAME in names, "gate job is missing the port-lane derivation step"
    claim_indices = [i for i, run in enumerate(runs) if CLAIMING_RUN_SNIPPET in run]
    assert claim_indices, f"gate job no longer runs {CLAIMING_RUN_SNIPPET!r}; update this test"

    lane_index = names.index(LANE_STEP_NAME)
    assert lane_index < min(claim_indices), (
        "the port-lane derivation step must run before the pool claim, "
        f"got lane step at {lane_index}, claim at {min(claim_indices)}"
    )


def _run_lane_script(runner_name: str | None) -> subprocess.CompletedProcess[str]:
    steps = _e2e_job_steps("gate")
    (lane_step,) = [step for step in steps if step.get("name") == LANE_STEP_NAME]
    script = lane_step["run"]
    env = dict(os.environ)
    env.pop("RUNNER_NAME", None)
    if runner_name is not None:
        env["RUNNER_NAME"] = runner_name
    with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as github_env_file:
        env["GITHUB_ENV"] = github_env_file.name
    try:
        result = subprocess.run(
            [posix_bash_or_skip(), "-c", script],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        result.stdout += Path(github_env_file.name).read_text(encoding="utf-8")
        return result
    finally:
        Path(github_env_file.name).unlink(missing_ok=True)


def _emitted_lane(result: subprocess.CompletedProcess[str]) -> int:
    match = re.search(r"^MUSIC_DJ_PORT_LANE=(\d+)$", result.stdout, re.MULTILINE)
    assert match, f"no MUSIC_DJ_PORT_LANE written to GITHUB_ENV: {result.stdout!r}"
    return int(match.group(1))


def test_suffixed_runner_names_derive_lane_from_the_trailing_index() -> None:
    """if agentbox-2 and agentbox-13 collapse onto one lane then the derivation is broken"""
    assert _run_lane_script("agentbox-2").returncode == 0
    assert _emitted_lane(_run_lane_script("agentbox-2")) == 3
    assert _emitted_lane(_run_lane_script("agentbox-13")) == 14
    assert _emitted_lane(_run_lane_script("nucbox-wsl-16")) == 17


def test_unsuffixed_runner_name_uses_lane_one_not_suffix_one_lane() -> None:
    """if the unsuffixed runner maps to suffix-one's lane then concurrent jobs
    collide, so it must use lane 1 while "nucbox-wsl-1" uses lane 2"""
    result = _run_lane_script("nucbox-wsl")
    assert result.returncode == 0, result.stderr
    lane = _emitted_lane(result)
    assert lane == 1
    assert lane != _emitted_lane(_run_lane_script("nucbox-wsl-1"))


def test_missing_runner_name_fails_closed_rather_than_guessing() -> None:
    """if RUNNER_NAME is unset then the step must fail loudly, not silently pick a lane
    that another, real runner could also resolve to"""
    result = _run_lane_script(None)
    assert result.returncode != 0, result.stdout
    assert "RUNNER_NAME is unset" in result.stderr


def test_every_e2e_job_fails_loudly_if_github_env_did_not_set_the_lane() -> None:
    """if a push, schedule, dispatch, or rerun loses the lane then server work stops"""
    for job_name in E2E_JOBS:
        steps = _e2e_job_steps(job_name)
        names = [step.get("name") for step in steps]
        assert LANE_STEP_NAME in names, f"{job_name} is missing lane derivation"
        assert LANE_ASSERTION_STEP_NAME in names, f"{job_name} is missing lane assertion"
        assert names.index(LANE_STEP_NAME) < names.index(LANE_ASSERTION_STEP_NAME), job_name
        assertion = next(step["run"] for step in steps if step.get("name") == LANE_ASSERTION_STEP_NAME)
        assert "MUSIC_DJ_PORT_LANE" in assertion
        assert "error" in assertion.lower()


def test_every_e2e_job_reclaims_stale_servers_after_lane_derivation() -> None:
    """if an old engine owns the lane then the next job logs and terminates it"""
    for job_name in E2E_JOBS:
        steps = _e2e_job_steps(job_name)
        names = [step.get("name") for step in steps]
        assert STALE_SERVER_HYGIENE_STEP_NAME in names, f"{job_name} lacks stale-server hygiene"
        assert names.index(LANE_ASSERTION_STEP_NAME) < names.index(STALE_SERVER_HYGIENE_STEP_NAME)
        script = next(step["run"] for step in steps if step.get("name") == STALE_SERVER_HYGIENE_STEP_NAME)
        assert script == "bash scripts/ci_e2e_port_hygiene.sh"
        helper = (REPO_ROOT / script.removeprefix("bash ")).read_text(encoding="utf-8")
        assert "apps.webui.server" in helper
        assert "vite" in helper
        assert "lsof" in helper
        assert "[e2e-hygiene] lane=" in helper
