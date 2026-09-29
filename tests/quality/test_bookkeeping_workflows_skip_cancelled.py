"""Bookkeeping workflow_run jobs skip cancelled triggering runs.

Reads the shipped workflows, not a second hand-maintained representation.

Regression lines:
  - if stable-evidence's batch pass filters on nothing but status, reads its mark from
    a cache, or can fail to overlap the previous pass, then broken
  - if trunk-job-verdict verdict runs on a cancelled trunk run, then broken
  - if ci-cost-guard's batch pass filters on conclusion or reads its mark from a
    cache, then broken
  - if two consecutive passes can fail to overlap, then
    broken (issue #2505: the collapse was considered and rejected because the
    guard prices one run id per invocation with no cross-run aggregation)
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
STABLE_EVIDENCE = REPO / ".github" / "workflows" / "stable-evidence.yml"
TRUNK_JOB_VERDICT = REPO / ".github" / "workflows" / "trunk-job-verdict.yml"
CI_COST_GUARD = REPO / ".github" / "workflows" / "ci-cost-guard.yml"

CANCELLED_SKIP = "github.event.workflow_run.conclusion != 'cancelled'"


def _workflow(path: Path) -> dict:
    assert path.is_file(), f"the workflow is missing: {path}"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict), f"{path.name} is not a mapping"
    return document


def _job_if(workflow: dict, job_name: str) -> str:
    job = workflow["jobs"][job_name]
    assert "if" in job, f"{job_name} job has no if field"
    return " ".join(str(job["if"]).split())


def _cadence_minutes(cron: str) -> int:
    match = re.fullmatch(r"(?:\*|\d+-59)/(\d+) \* \* \* \*", cron)
    assert match, f"not an every-N-minutes schedule: {cron}"
    return int(match.group(1))


def _assert_batch_pass(
    workflow: dict, job_name: str, workflow_file: str, lookback_floor_min: int
) -> None:
    """One scheduled pass, the mark read from GitHub's record of this workflow, overlap
    of at least two cadences, one concurrency group with no expression in it."""
    steps = workflow["jobs"][job_name]["steps"]
    mark = next(step for step in steps if step.get("id") == "mark")
    # if a failed pass advances the mark, or the search stops at a fixed window, then a run
    # of failed passes skips completions; the tested reader pages the whole history
    assert "scripts.ci_run_batch mark" in mark["run"], "the mark comes from the tested reader"
    assert f"--workflow-file {workflow_file}" in mark["run"]
    assert "per_page" not in mark["run"] and "failure" not in mark["run"]
    cadence = _cadence_minutes(workflow[True]["schedule"][0]["cron"])
    env = workflow["jobs"][job_name]["env"]
    assert int(env["OVERLAP_MINUTES"]) >= 2 * cadence
    assert int(env["LOOKBACK_HOURS"]) * 60 >= lookback_floor_min
    concurrency = workflow["concurrency"]
    assert concurrency["cancel-in-progress"] is False
    assert "${{" not in str(concurrency["group"]), "one pass at a time, one group"


def test_stable_evidence_batch_selects_in_the_script_not_the_workflow() -> None:
    """The cancelled and `CI` dispatch exclusions live in select_suite_runs (pinned by
    tests/scripts/test_stable_evidence_batch.py); the workflow hands the script no
    conclusion, no event, and no per-run job condition."""
    workflow = _workflow(STABLE_EVIDENCE)
    assert "workflow_run" not in workflow[True]
    job = workflow["jobs"]["append"]
    assert "if" not in job
    batch = next(step for step in job["steps"] if step.get("id") == "batch")
    assert "scripts.stable_evidence_batch" in batch["run"]
    assert "conclusion" not in batch["run"]
    _assert_batch_pass(workflow, "append", "stable-evidence.yml", 240)


def test_trunk_job_verdict_skips_cancelled_triggering_run() -> None:
    """if a trunk CI or E2E run is cancelled then the verdict job does not allocate a runner."""
    workflow = _workflow(TRUNK_JOB_VERDICT)
    trigger = workflow[True]["workflow_run"]
    assert trigger["types"] == ["completed"]

    condition = _job_if(workflow, "verdict")
    assert "github.event.repository.default_branch" in condition
    assert CANCELLED_SKIP in condition


def test_ci_cost_guard_batch_lists_every_completion_including_cancelled() -> None:
    """The batch pass filters on status=completed and never on conclusion.

    A run cancelled at its timeout bills the whole ceiling, which is the case
    the threshold is sized for (#2505). tests/test_ci_cost_guard_batch.py pins
    the selection; this pins that the workflow hands the script no conclusion
    filter and that the mark is read from GitHub's own record of this
    workflow, not from a cache that can be evicted.
    """
    workflow = _workflow(CI_COST_GUARD)
    steps = workflow["jobs"]["assess"]["steps"]
    price = next(step for step in steps if step.get("id") == "guard")
    assert "--batch" in price["run"]
    assert "conclusion" not in price["run"]
    mark = next(step for step in steps if step.get("id") == "mark")
    assert "--workflow-file ci-cost-guard.yml" in mark["run"]


def test_ci_cost_guard_passes_overlap_by_at_least_one_cadence() -> None:
    """Two consecutive passes must overlap, so a late or failed pass loses nothing.

    The overlap floor (OVERLAP_MINUTES) must be at least twice the cron
    cadence, and the run creation lookback (LOOKBACK_HOURS) must exceed the
    longest watched workflow timeout, or a run created before the mark and
    completed after it is never priced. E2E's extended job alone can run 45 + 30 min.
    """
    _assert_batch_pass(_workflow(CI_COST_GUARD), "assess", "ci-cost-guard.yml", 120)
