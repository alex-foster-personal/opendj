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
  - if the upload job stages a report outside runner.temp, then broken (a report an
    earlier run left in the persistent workspace is recorded under this SHA)
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from tests.scripts.ci_runner_routes import MAIN_TOKEN_RUNS_ON, TOKEN_RUNS_ON, without_main_reserve

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


def test_the_upload_job_is_the_light_pool_job_downstream_of_the_shards() -> None:
    """CONTROL: the cancellation assertions below are about the gated job that
    waits on the pytest shards, now on the self-hosted light pool
    (ADR-NEW-trunk-token-jobs-self-hosted), where a skipped start saves a runner
    seat instead of a hosted minute. A push to main prefers the opt-in
    CI_RUNS_ON_MAIN reserve first (#8248, tests/scripts/test_ci_main_push_runner_reserve.py);
    every other event still takes the light pool route."""
    job_id, job = _upload_job()
    runs_on = job.get("runs-on")
    assert runs_on == MAIN_TOKEN_RUNS_ON, f"{job_id} left the main-reserved light pool route"
    assert without_main_reserve(runs_on) == TOKEN_RUNS_ON, f"{job_id} left the light pool route"
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


def test_the_upload_job_stages_reports_where_the_runner_empties_them() -> None:
    """The workspace persists on self-hosted runners, but runner.temp is emptied at
    the start of each job, so a failed download cannot leave an earlier run's report."""
    job_id, job = _upload_job()
    downloads = [s for s in job["steps"] if "--aws-sigv4" in str(s.get("run"))]
    uploads = [s for s in job["steps"] if "analytics-uploader" in str(s.get("uses"))]
    finds = [s for s in job["steps"] if re.fullmatch(r"(shard|rust)-report-.+", str(s.get("id")))]
    assert len(downloads) == len(uploads) == len(finds) == 7, (len(downloads), len(uploads), len(finds))
    for step in downloads:
        assert step["env"]["DEST"].startswith("${{ runner.temp }}/staged-"), f"{job_id}: {step['env']['DEST']}"
    for step in uploads:
        assert step["with"]["junit-paths"].startswith("${{ runner.temp }}/staged-"), f"{job_id}: {step['with']['junit-paths']}"
    for step in finds:
        assert '"$RUNNER_TEMP/staged-' in step["run"], f"{step['id']} looks in the persistent workspace"
