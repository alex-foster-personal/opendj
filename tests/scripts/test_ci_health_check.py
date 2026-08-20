"""Tests for :mod:`scripts.ci_health_check`, the out-of-CI health watchdog.

Every case here is one of the natural-language acceptance tests written into the
module docstring's mini-PRD. The billing fixtures reproduce the real signature
observed on this repo Tue 18 Aug - Wed 19 Aug 2026: every workflow failing in
4-5s with zero steps executed.

No network: the check functions are pure over a list of Run objects, so the gh
plumbing is never exercised here.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from scripts import ci_health_check as mod

NOW = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)


def _run(
    *,
    age_minutes: int,
    duration_seconds: float,
    conclusion: str,
    branch: str = "main",
    event: str = "push",
) -> mod.Run:
    started = NOW - timedelta(minutes=age_minutes)
    return mod.Run(
        run_id=age_minutes,
        name="CI",
        event=event,
        head_branch=branch,
        conclusion=conclusion,
        started_at=started,
        updated_at=started + timedelta(seconds=duration_seconds),
    )


def _runs(count: int, *, duration_seconds: float, conclusion: str, branch: str = "main"):
    return [
        _run(
            age_minutes=index + 1,
            duration_seconds=duration_seconds,
            conclusion=conclusion,
            branch=branch,
        )
        for index in range(count)
    ]


# ----- R1 billing signature ---------------------------------------------------------


def test_billing_fires_on_instant_uniform_failures():
    """If the 8 newest runs all failed with a 4s median then billing, exit 2, or broken."""
    result = mod._check_billing(_runs(8, duration_seconds=4, conclusion="failure"))
    assert not result.ok
    assert result.classification == "billing"
    assert result.exit_code == mod.EXIT_BILLING
    assert result.sample_size == 8
    assert "github.com/settings/billing" in result.remediation


def test_billing_stays_quiet_on_slow_real_failures():
    """If all 8 newest runs failed but took 200s
    then that is real breakage, not refusal, or broken.
    """
    result = mod._check_billing(_runs(8, duration_seconds=200, conclusion="failure"))
    assert result.ok
    assert result.classification == "healthy"


def test_billing_stays_quiet_when_some_runs_succeed():
    """If any of the 8 newest runs succeeded then the runner is not refusing, or broken."""
    sample = _runs(7, duration_seconds=4, conclusion="failure")
    sample.append(_run(age_minutes=8, duration_seconds=4, conclusion="success"))
    result = mod._check_billing(sample)
    assert result.ok


def test_billing_refuses_to_pass_on_thin_sample():
    """If fewer than BILLING_MIN_SAMPLE runs exist
    then insufficient-data ERROR, never a silent pass, or broken.
    """
    result = mod._check_billing(_runs(2, duration_seconds=4, conclusion="failure"))
    assert not result.ok
    assert result.classification == "insufficient-data"
    assert result.sample_size == 2


# ----- R2 trigger drift -------------------------------------------------------------


def test_trigger_drift_fires_when_commits_land_but_nothing_runs(monkeypatch):
    """If commits landed on main in 48h and zero runs carry head_branch main
    then drift, exit 3, or broken.
    """
    monkeypatch.setattr(mod, "_fetch_default_branch_commit_count", lambda branch, since: 6)
    feature_runs = _runs(5, duration_seconds=300, conclusion="success", branch="af--feature")
    result = mod._check_trigger_drift(feature_runs, "main", NOW)
    assert not result.ok
    assert result.classification == "trigger-drift"
    assert result.exit_code == mod.EXIT_TRIGGER_DRIFT
    assert ".github/workflows" in result.remediation


def test_trigger_drift_quiet_when_runs_match_commits(monkeypatch):
    """If commits landed and runs started on main then triggers are firing, or broken."""
    monkeypatch.setattr(mod, "_fetch_default_branch_commit_count", lambda branch, since: 6)
    result = mod._check_trigger_drift(
        _runs(6, duration_seconds=300, conclusion="success"), "main", NOW
    )
    assert result.ok


def test_trigger_drift_quiet_on_a_repo_with_no_pushes(monkeypatch):
    """If no commits landed in the window then silence is expected, not drift, or broken."""
    monkeypatch.setattr(mod, "_fetch_default_branch_commit_count", lambda branch, since: 0)
    result = mod._check_trigger_drift([], "main", NOW)
    assert result.ok


def test_trigger_drift_ignores_runs_older_than_the_window(monkeypatch):
    """If the only main runs predate the 48h window then drift still fires, or broken."""
    monkeypatch.setattr(mod, "_fetch_default_branch_commit_count", lambda branch, since: 3)
    stale = [_run(age_minutes=60 * 24 * 5, duration_seconds=300, conclusion="success")]
    result = mod._check_trigger_drift(stale, "main", NOW)
    assert not result.ok
    assert result.classification == "trigger-drift"


# ----- R3 real failure rate ---------------------------------------------------------


def test_failure_rate_fires_above_the_limit():
    """If 14 of 20 runs failed after 300s then 70 percent, exit 4, or broken."""
    sample = _runs(14, duration_seconds=300, conclusion="failure")
    sample += [
        _run(age_minutes=100 + index, duration_seconds=300, conclusion="success")
        for index in range(6)
    ]
    result = mod._check_failure_rate(sample)
    assert not result.ok
    assert result.exit_code == mod.EXIT_FAILURE_RATE
    assert result.sample_size == 20
    assert "70 percent" in result.detail


def test_failure_rate_excludes_instant_refusals():
    """If all 20 runs failed in 4s
    then failure-rate stays OK and billing carries the alarm, or broken.
    """
    result = mod._check_failure_rate(_runs(20, duration_seconds=4, conclusion="failure"))
    assert result.ok
    assert "0/20" in result.detail


def test_failure_rate_quiet_on_a_healthy_repo():
    """If 2 of 20 runs failed after 300s then 10 percent is under the limit, or broken."""
    sample = _runs(2, duration_seconds=300, conclusion="failure")
    sample += [
        _run(age_minutes=100 + index, duration_seconds=300, conclusion="success")
        for index in range(18)
    ]
    result = mod._check_failure_rate(sample)
    assert result.ok


def test_failure_rate_refuses_to_pass_with_no_runs():
    """If there are zero completed runs
    then insufficient-data ERROR, never a silent pass, or broken.
    """
    result = mod._check_failure_rate([])
    assert not result.ok
    assert result.classification == "insufficient-data"


# ----- R4 staleness -----------------------------------------------------------------


def test_staleness_fires_when_pushes_land_but_ci_is_silent(monkeypatch):
    """If the newest run is 9 days old and commits landed 3 days ago
    then stale, exit 5, or broken.
    """
    monkeypatch.setattr(mod, "_fetch_default_branch_commit_count", lambda branch, since: 4)
    ancient = [_run(age_minutes=60 * 24 * 9, duration_seconds=300, conclusion="success")]
    result = mod._check_staleness(ancient, "main", NOW)
    assert not result.ok
    assert result.classification == "staleness"
    assert result.exit_code == mod.EXIT_STALENESS


def test_staleness_quiet_when_ci_ran_recently(monkeypatch):
    """If a run completed 2 hours ago then CI is alive, or broken."""
    monkeypatch.setattr(mod, "_fetch_default_branch_commit_count", lambda branch, since: 4)
    result = mod._check_staleness(
        [_run(age_minutes=120, duration_seconds=300, conclusion="success")], "main", NOW
    )
    assert result.ok


def test_staleness_quiet_on_a_dormant_repo(monkeypatch):
    """If nothing ran and nothing was pushed in 7 days then dormant is not a fault, or broken."""
    monkeypatch.setattr(mod, "_fetch_default_branch_commit_count", lambda branch, since: 0)
    result = mod._check_staleness([], "main", NOW)
    assert result.ok


# ----- R5 severity ordering and machine-readable output -----------------------------


def test_exit_code_leads_with_the_most_severe_failure():
    """If billing and failure-rate both fail then the exit code is billing's, or broken."""
    results = [
        mod.CheckResult("billing", False, "billing", "d", 8, mod.EXIT_BILLING),
        mod.CheckResult("failure-rate", False, "failure-rate", "d", 20, mod.EXIT_FAILURE_RATE),
    ]
    assert mod._resolve_exit_code(results) == mod.EXIT_BILLING


def test_exit_code_is_zero_when_everything_passes():
    """If every check is ok then exit 0, or broken."""
    results = [mod.CheckResult("billing", True, "healthy", "d", 8, mod.EXIT_OK)]
    assert mod._resolve_exit_code(results) == mod.EXIT_OK


def test_dynamic_events_are_excluded_from_the_run_pool():
    """If 'dynamic' dependency-graph runs were counted
    then they would mask the billing signature, or broken.
    """
    assert "dynamic" in mod.EXCLUDED_RUN_EVENTS


def test_verdict_line_uses_house_output_tokens():
    """If a verdict line does not start with [OK] or [ERROR]
    then the notifier cannot parse it, or broken.
    """
    ok_line = mod.CheckResult("billing", True, "healthy", "fine", 8, mod.EXIT_OK).verdict_line()
    bad_line = mod.CheckResult(
        "billing", False, "billing", "bad", 8, mod.EXIT_BILLING
    ).verdict_line()
    assert ok_line.startswith("[OK] billing:")
    assert bad_line.startswith("[ERROR] billing:")


@pytest.mark.parametrize(
    "field,value",
    [("run_started_at", "not-a-date"), ("updated_at", None)],
)
def test_malformed_timestamps_fail_loudly(field, value):
    """If GitHub returns an unparseable timestamp
    then raise, never coerce to a default, or broken.
    """
    with pytest.raises(mod.PreconditionError) as excinfo:
        mod._parse_github_timestamp(value, field, 123)
    assert field in str(excinfo.value)


# ----- R7 iteration-speed regression ------------------------------------------------


def _metric(step: str, seconds: float, *, job_id: int | None = None) -> mod.Metric:
    return mod.Metric(
        ts="2026-08-20T00:00:00Z", step=step, seconds=seconds, source="ci", job_id=job_id
    )


def _history(step: str, seconds: float, count: int) -> list[mod.Metric]:
    return [_metric(step, seconds) for _ in range(count)]


def _series(step: str, seconds: float, count: int, newest: float) -> list[mod.Metric]:
    """`count` prior records at `seconds`, then the one newest record under test."""
    return [*_history(step, seconds, count), _metric(step, newest)]


def test_iteration_speed_fires_when_a_step_doubles():
    """If a step's newest run took 40s against a 20s median then exit 6, or broken."""
    metrics = _series("ci:CI/pytest", 20.0, 20, 40.0)
    result = mod._check_iteration_speed(metrics)
    assert not result.ok
    assert result.classification == "iteration-speed"
    assert result.exit_code == mod.EXIT_ITERATION_SPEED
    assert result.sample_size == 20
    assert "2.00x" in result.detail


def test_iteration_speed_quiet_just_under_the_factor():
    """If the newest run is 1.25x the median then that is under 1.5x, or broken."""
    metrics = _series("ci:CI/pytest", 20.0, 20, 25.0)
    assert mod._check_iteration_speed(metrics).ok


def test_iteration_speed_fires_just_over_the_factor():
    """If the newest run is 1.6x the median then the boundary is live, or broken."""
    metrics = _series("ci:CI/pytest", 20.0, 20, 32.0)
    assert not mod._check_iteration_speed(metrics).ok


def test_newest_record_is_excluded_from_its_own_median():
    """If the newest sample were inside its own median
    then a regression would blunt the baseline it is measured against, or broken.
    """
    # 5 prior records at 20s. Including the 200s newest would drag the median upward and
    # could hide the very spike being looked for.
    metrics = _series("ci:CI/pytest", 20.0, 5, 200.0)
    result = mod._check_iteration_speed(metrics)
    assert not result.ok
    assert "20.0s median" in result.detail
    assert result.sample_size == 5


def test_iteration_speed_ignores_steps_with_too_little_history():
    """If a step has fewer than ITERATION_SPEED_MIN_SAMPLE prior records
    then no ratio is computable and it must not alert, or broken.
    """
    metrics = _series("ci:CI/pytest", 20.0, 3, 400.0)
    result = mod._check_iteration_speed(metrics)
    assert result.ok
    assert result.classification == "insufficient-data"


def test_thin_history_is_reported_not_silently_passed():
    """If insufficient data produced an empty detail
    then the shortfall would be invisible in the verdict line, or broken.
    """
    result = mod._check_iteration_speed(_history("ci:CI/pytest", 20.0, 2))
    assert str(mod.ITERATION_SPEED_MIN_SAMPLE) in result.detail
    assert "no regression is computable" in result.detail


def test_fast_steps_are_never_judged():
    """If a 2s step tripling to 6s alerted then scheduler jitter would page the maintainer, or broken."""
    metrics = _series("vite-hmr", 2.0, 20, 6.0)
    result = mod._check_iteration_speed(metrics)
    assert result.ok
    assert result.classification == "insufficient-data"


def test_median_window_ignores_records_older_than_the_window():
    """If more than ITERATION_SPEED_MEDIAN_WINDOW records fed the median
    then ancient timings would anchor it forever, or broken.
    """
    ancient = _history("ci:CI/pytest", 1000.0, 40)
    recent = _history("ci:CI/pytest", 20.0, 20)
    result = mod._check_iteration_speed([*ancient, *recent, _metric("ci:CI/pytest", 40.0)])
    assert not result.ok
    assert result.sample_size == mod.ITERATION_SPEED_MEDIAN_WINDOW


def test_worst_offender_is_the_one_reported():
    """If a milder regression outranked a worse one then the alert would misdirect, or broken."""
    metrics = [
        *_series("ci:CI/mild", 20.0, 20, 34.0),
        *_series("ci:CI/severe", 20.0, 20, 100.0),
    ]
    result = mod._check_iteration_speed(metrics)
    assert not result.ok
    assert "ci:CI/severe" in result.detail


def test_healthy_verdict_names_how_many_steps_were_judged():
    """If the healthy detail hid its denominator then the rate would be unattributable."""
    metrics = _series("ci:CI/pytest", 20.0, 20, 21.0)
    result = mod._check_iteration_speed(metrics)
    assert result.ok
    assert result.classification == "healthy"
    assert "1 of 1 steps" in result.detail


def test_iteration_speed_is_the_least_severe_failure():
    """If iteration speed outranked billing then a slow build would mask an outage, or broken."""
    results = [
        mod.CheckResult("billing", False, "billing", "d", 8, mod.EXIT_BILLING),
        mod.CheckResult(
            "iteration-speed", False, "iteration-speed", "d", 20, mod.EXIT_ITERATION_SPEED
        ),
    ]
    assert mod._resolve_exit_code(results) == mod.EXIT_BILLING
    assert mod.EXIT_ITERATION_SPEED > mod.EXIT_STALENESS


# ----- metrics store parsing --------------------------------------------------------


def test_absent_store_is_not_an_error(tmp_path):
    """If a machine that has never timed anything raised
    then the watchdog would fail on first install, or broken.
    """
    assert mod._read_metrics(tmp_path / "nothing-here.jsonl") == []


def test_malformed_store_line_fails_loudly(tmp_path):
    """If a corrupt line were skipped then the median would silently drift, or broken."""
    path = tmp_path / "metrics.jsonl"
    path.write_text('{"ts":"t","step":"a","seconds":1}\nnot json at all\n')
    with pytest.raises(mod.PreconditionError) as excinfo:
        mod._read_metrics(path)
    assert "metrics.jsonl:2" in str(excinfo.value)


def test_store_line_missing_required_keys_fails_loudly(tmp_path):
    """If a line without seconds were accepted then it would break the median, or broken."""
    path = tmp_path / "metrics.jsonl"
    path.write_text('{"ts":"t","step":"a"}\n')
    with pytest.raises(mod.PreconditionError) as excinfo:
        mod._read_metrics(path)
    assert "seconds" in str(excinfo.value)


def test_non_numeric_seconds_fails_loudly(tmp_path):
    """If seconds were a string then statistics.median would explode later, or broken."""
    path = tmp_path / "metrics.jsonl"
    path.write_text('{"ts":"t","step":"a","seconds":"quick"}\n')
    with pytest.raises(mod.PreconditionError):
        mod._read_metrics(path)


def test_blank_lines_are_tolerated(tmp_path):
    """If a trailing newline raised then every well-formed store would fail, or broken."""
    path = tmp_path / "metrics.jsonl"
    path.write_text('{"ts":"t","step":"a","seconds":1}\n\n')
    assert len(mod._read_metrics(path)) == 1


# ----- CI job recording -------------------------------------------------------------


def _job(job_id: int, *, seconds: float, name: str = "pytest") -> mod.Job:
    started = NOW - timedelta(seconds=seconds)
    return mod.Job(
        job_id=job_id,
        workflow="CI",
        name=name,
        head_sha="abc1234",
        conclusion="success",
        started_at=started,
        completed_at=started + timedelta(seconds=seconds),
    )


def test_job_step_key_is_namespaced_away_from_local_steps():
    """If a CI job shared a key with a local `just` step
    then two different machines' timings would share one median, or broken.
    """
    assert _job(1, seconds=10).step == "ci:CI/pytest"


def test_recording_appends_new_jobs(tmp_path):
    """If new jobs were not written then check 5 would have nothing to read, or broken."""
    path = tmp_path / "metrics.jsonl"
    written = mod._record_job_metrics([_job(1, seconds=30), _job(2, seconds=40)], [], path)
    assert written == 2
    records = mod._read_metrics(path)
    assert [record.seconds for record in records] == [30.0, 40.0]
    assert all(record.source == mod.CI_METRIC_SOURCE for record in records)


def test_recording_skips_jobs_already_in_the_store(tmp_path):
    """If the 4-hourly poll re-recorded the same job
    then the median would describe the cadence, not the build, or broken.
    """
    path = tmp_path / "metrics.jsonl"
    mod._record_job_metrics([_job(1, seconds=30)], [], path)
    existing = mod._read_metrics(path)
    written = mod._record_job_metrics([_job(1, seconds=30), _job(2, seconds=40)], existing, path)
    assert written == 1
    assert len(mod._read_metrics(path)) == 2


def test_recording_is_a_no_op_when_everything_is_known(tmp_path):
    """If a fully-seen batch still opened the file then empty writes would churn it."""
    path = tmp_path / "metrics.jsonl"
    existing = [_metric("ci:CI/pytest", 30.0, job_id=1)]
    assert mod._record_job_metrics([_job(1, seconds=30)], existing, path) == 0
    assert not path.exists()


def test_failed_job_is_recorded_with_a_nonzero_exit(tmp_path):
    """If a failed job were filed as exit 0 then slow and broken would be indistinguishable."""
    path = tmp_path / "metrics.jsonl"
    job = _job(1, seconds=30)
    job.conclusion = "failure"
    mod._record_job_metrics([job], [], path)
    assert json.loads(path.read_text().splitlines()[0])["exit"] == 1


# ----- skipped jobs and negative durations ------------------------------------------
#
# Found live Thu 20 Aug 2026: the first real run of check 5 recorded 'ci:CI/quality
# ratchet' at -1.0s and 'ci:CI/Windows Rekordbox parity gate' at -11.0s. Both were
# conclusion 'skipped', and GitHub stamps a skipped job's completed_at one to eleven
# seconds BEFORE its started_at. Left alone, negative numbers would sit in the median that
# every future run is judged against.


def _jobs_payload(monkeypatch, items):
    """Stub the gh layer so _fetch_jobs runs over a literal jobs payload."""
    run = _run(age_minutes=1, duration_seconds=100, conclusion="success")
    monkeypatch.setattr(mod, "_gh_api_json", lambda path: {"jobs": items})
    return [run]


def _job_item(job_id, *, conclusion, started, completed, name="quality ratchet"):
    return {
        "id": job_id,
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
        "head_sha": "abc1234def",
        "started_at": started,
        "completed_at": completed,
    }


def test_skipped_jobs_are_never_recorded(monkeypatch):
    """If a skipped job entered the store then a negative duration would poison the median."""
    runs = _jobs_payload(
        monkeypatch,
        [
            _job_item(
                1,
                conclusion="skipped",
                started="2026-08-20T18:21:09Z",
                completed="2026-08-20T18:21:08Z",
            )
        ],
    )
    assert mod._fetch_jobs(runs) == []


@pytest.mark.parametrize("conclusion", ["skipped", "cancelled", "neutral"])
def test_non_executing_conclusions_are_all_excluded(monkeypatch, conclusion):
    """If a job that never ran were timed then the number would describe nothing."""
    runs = _jobs_payload(
        monkeypatch,
        [
            _job_item(
                1,
                conclusion=conclusion,
                started="2026-08-20T18:00:00Z",
                completed="2026-08-20T18:00:30Z",
            )
        ],
    )
    assert mod._fetch_jobs(runs) == []


def test_executed_jobs_are_still_recorded(monkeypatch):
    """If the skip filter also dropped real jobs then check 5 would starve, or broken."""
    runs = _jobs_payload(
        monkeypatch,
        [
            _job_item(
                1,
                conclusion="success",
                started="2026-08-20T18:00:00Z",
                completed="2026-08-20T18:00:30Z",
            )
        ],
    )
    jobs = mod._fetch_jobs(runs)
    assert len(jobs) == 1
    assert jobs[0].duration_seconds == 30.0


def test_negative_duration_on_an_executed_job_fails_loudly(monkeypatch):
    """If an unexplained negative duration were dropped quietly
    then a change in GitHub's timing semantics would hide behind a thinning sample.
    """
    runs = _jobs_payload(
        monkeypatch,
        [
            _job_item(
                77,
                conclusion="success",
                started="2026-08-20T18:00:30Z",
                completed="2026-08-20T18:00:00Z",
            )
        ],
    )
    with pytest.raises(mod.PreconditionError) as excinfo:
        mod._fetch_jobs(runs)
    assert "negative duration" in str(excinfo.value)
    assert "77" in str(excinfo.value)


def test_incomplete_jobs_are_skipped(monkeypatch):
    """If an in-progress job were timed then a half-finished run would set the median."""
    item = _job_item(
        1, conclusion="success", started="2026-08-20T18:00:00Z", completed="2026-08-20T18:00:30Z"
    )
    item["status"] = "in_progress"
    item["completed_at"] = None
    assert mod._fetch_jobs(_jobs_payload(monkeypatch, [item])) == []
