"""The hosted test-analytics upload job does not start on a cancelled CI run.

Reads the shipped workflow, not a second hand-maintained representation.
ADR-NEW-ci-insights-skip-cancelled-runs: measured Mon 28 Sep 2026 15:00Z to
Tue 29 Sep 03:00Z, 122 of 254 CI runs were cancelled (a newer push superseded
them), and the upload job still started on every one under `always()`, billing
a hosted minute to download reports that were never written.

The job is found by its gate variable, not by its id, so a rename of the job
keeps this test pointed at it.

Regression lines:
  - if the upload job runs on a cancelled workflow run again, then broken (one
    hosted minute per superseded push, for nothing)
  - if the upload job is skipped when a pytest shard FAILS, then broken (a red
    shard's test health is the data most worth recording)
  - if no job is gated on vars.CI_INSIGHTS_ENABLED, then broken (this test would
    be proving a property of a job that no longer exists)
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
CI = REPO / ".github" / "workflows" / "ci.yml"
GATE_VARIABLE = "vars.CI_INSIGHTS_ENABLED"


def _upload_job() -> tuple[str, dict]:
    assert CI.is_file(), "ci workflow is missing"
    document = yaml.safe_load(CI.read_text(encoding="utf-8"))
    assert isinstance(document, dict), "ci.yml is not a mapping"
    gated = {
        job_id: job
        for job_id, job in document["jobs"].items()
        if GATE_VARIABLE in str(job.get("if", ""))
    }
    assert len(gated) == 1, f"expected one job gated on {GATE_VARIABLE}, got {sorted(gated)}"
    return next(iter(gated.items()))


def test_the_upload_job_is_the_hosted_job_downstream_of_the_shards() -> None:
    """CONTROL: the cancellation assertions below only save money while the gated
    job is the hosted one that waits on the pytest shards."""
    job_id, job = _upload_job()
    assert job.get("runs-on") == "ubuntu-latest", f"{job_id} is no longer hosted"
    assert "test" in (job["needs"] if isinstance(job["needs"], list) else [job["needs"]]), (
        f"{job_id} no longer waits on the pytest shards"
    )


def test_the_upload_job_does_not_start_on_a_cancelled_run() -> None:
    job_id, job = _upload_job()
    condition = str(job["if"])
    assert "always()" not in condition, (
        f"{job_id} is gated on always(), which starts it (and bills a hosted "
        "minute) on a run a newer push cancelled"
    )
    assert "!cancelled()" in condition, (
        f"{job_id} has no !cancelled() status check, so a failing shard would "
        "skip the upload under the implicit success()"
    )


def test_the_upload_job_still_runs_when_a_shard_fails() -> None:
    job_id, job = _upload_job()
    condition = str(job["if"])
    assert "success()" not in condition.replace("!cancelled()", ""), (
        f"{job_id} requires success(), so a red shard's test health is dropped"
    )
