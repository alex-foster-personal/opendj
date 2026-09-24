"""The lockfile check is hosted-only and gated on the account's hosted billing.

- [if] GitHub-hosted Actions are blocked on billing [then] the job is skipped, never
  a failure that reads as a lockfile verdict, [else stop]
- [if] the gate variable is set [then] the job runs on ubuntu-latest exactly as before,
  [else stop]
- [if] the hosted job is skipped [then] the metadata half still runs, ungated, on the
  self-hosted pool with no resolution, so a stale lock stays red (Codex P1, #3763),
  [else stop]
"""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "lockfile-check.yml"
GATE = "vars.CI_HOSTED_LOCKFILE_CHECK == 'true'"


def test_lockfile_check_is_gated_on_hosted_billing() -> None:
    """if the gate is dropped then a billing block reads as a red lockfile verdict"""
    job = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["uv-lock"]
    condition = " ".join(str(job.get("if", "")).split())
    assert GATE in condition, (
        "the hosted lockfile job must skip while hosted billing is blocked, "
        f"not fail with no runner:\n{condition!r}"
    )
    assert job["runs-on"] == "ubuntu-latest", "the check stays hosted (sdist exposure)"
    assert "uv lock --check" in job["steps"][-1]["run"]


LINUX_POOL = "${{ fromJSON(vars.CI_RUNS_ON_LINUX || '\"ubuntu-latest\"') }}"


def test_metadata_check_runs_ungated_on_the_pool() -> None:
    """if the metadata job is gated or dropped then a billing block makes a stale lock
    mergeable again, which is the #2740 incident this workflow exists to stop"""
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    assert "lock-metadata" in jobs, "the ungated metadata half is missing"
    job = jobs["lock-metadata"]
    assert "if" not in job, f"the metadata half must never be gated: {job.get('if')!r}"
    assert job["runs-on"] == LINUX_POOL, job["runs-on"]
    runs = [s.get("run", "") for s in job["steps"]]
    assert any("python -m scripts.lock_metadata_check" in r for r in runs), runs
    assert not any("uv lock" in r for r in runs), "no resolution on the persistent pool"


def test_uv_toml_triggers_both_halves() -> None:
    """if uv.toml leaves either trigger list then a PR that adds or edits it, which
    outranks [tool.uv] for every compared setting, runs no lock check at all (Codex P2
    on #3763, round 57); with it listed the metadata job runs and reports UNKNOWN"""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = workflow.get("on", workflow.get(True))  # PyYAML reads a bare `on` as True
    for event in ("pull_request", "push"):
        paths = triggers[event]["paths"]
        assert "uv.toml" in paths, (event, paths)
        assert "pyproject.toml" in paths and "uv.lock" in paths, (event, paths)
