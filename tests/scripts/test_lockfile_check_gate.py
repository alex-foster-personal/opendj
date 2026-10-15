"""Both halves of the lockfile check run, ungated, on the self-hosted pool.

ADR-NEW-security-scans-on-self-hosted-pool, Thu 15 Oct 2026. The resolution half used
to be GitHub-hosted and gated on vars.CI_HOSTED_LOCKFILE_CHECK, which stayed unset after
the Mon 21 Sep 2026 billing block, so it skipped on every PR for over three weeks.

- [if] the resolution half is gated on a repo variable again [then] fail, [else stop]
- [if] it runs `uv lock --check` without --no-build on the pool [then] fail: a source
  build runs package code on a persistent runner, [else stop]
- [if] its uv cache is not the job's RUNNER_TEMP [then] fail: a download would outlive
  the job, [else stop]
- [if] the metadata half is gated or resolves [then] fail (Codex P1, #3763), [else stop]
"""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "lockfile-check.yml"
LINUX_POOL = "${{ fromJSON(vars.CI_RUNS_ON_LINUX || '\"ubuntu-latest\"') }}"


def test_resolution_check_runs_ungated_on_the_pool_without_builds() -> None:
    """if the gate returns then a forgotten variable silently skips the check again;
    if --no-build goes then a dependency PR's sdist build runs code on the pool"""
    job = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["uv-lock"]
    assert "if" not in job, f"the resolution half must never be gated: {job.get('if')!r}"
    assert job["runs-on"] == LINUX_POOL, job["runs-on"]
    check = [s for s in job["steps"] if "uv lock" in s.get("run", "")]
    assert len(check) == 1, check
    assert check[0]["run"].split() == ["uv", "lock", "--check", "--no-build"], check[0]["run"]
    assert check[0]["env"]["UV_CACHE_DIR"] == "${{ runner.temp }}/uv-cache", check[0]
    setup = [s for s in job["steps"] if str(s.get("uses", "")).startswith("astral-sh/setup-uv@")]
    assert setup and setup[0]["with"]["enable-cache"] is False, setup


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
