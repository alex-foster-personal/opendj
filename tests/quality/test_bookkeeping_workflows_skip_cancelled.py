"""Bookkeeping workflow_run jobs skip cancelled triggering runs.

Reads the shipped workflows, not a second hand-maintained representation.

Regression lines:
  - if stable-evidence append runs on a cancelled CI/E2E run, then broken
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


def test_stable_evidence_append_skips_cancelled_triggering_run() -> None:
    """if CI or E2E is cancelled then stable-evidence append does not allocate a runner."""
    workflow = _workflow(STABLE_EVIDENCE)
    trigger = workflow[True]["workflow_run"]
    assert trigger["types"] == ["completed"]

    condition = _job_if(workflow, "append")
    assert CANCELLED_SKIP in condition


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
    assert "workflows/ci-cost-guard.yml/runs" in mark["run"]
    assert "run_started_at" in mark["run"]


def test_ci_cost_guard_passes_overlap_by_at_least_one_cadence() -> None:
    """Two consecutive passes must overlap, so a late or failed pass loses nothing.

    The overlap floor (OVERLAP_MINUTES) must be at least twice the cron
    cadence, and the run creation lookback (LOOKBACK_HOURS) must exceed the
    longest watched workflow timeout, or a run created before the mark and
    completed after it is never priced.
    """
    workflow = _workflow(CI_COST_GUARD)
    cron = workflow[True]["schedule"][0]["cron"]
    match = re.fullmatch(r"\*/(\d+) \* \* \* \*", cron)
    assert match, f"the guard's cron is not an every-N-minutes schedule: {cron}"
    cadence = int(match.group(1))
    env = workflow["jobs"]["assess"]["env"]
    assert int(env["OVERLAP_MINUTES"]) >= 2 * cadence
    assert int(env["LOOKBACK_HOURS"]) * 60 >= 120, "E2E's extended job alone can run 45 + 30 min"
    concurrency = workflow["concurrency"]
    assert concurrency["cancel-in-progress"] is False
    assert "${{" not in str(concurrency["group"]), "one pass at a time, one group"
