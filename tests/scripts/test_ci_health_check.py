"""Tests for :mod:`scripts.ci_health_check`, the out-of-CI health watchdog.

Every case here is one of the natural-language acceptance tests written into the
module docstring's mini-PRD. The billing fixtures reproduce the real signature
observed on this repo Tue 18 Aug - Wed 19 Aug 2026: every workflow failing in
4-5s with zero steps executed.

No network: the check functions are pure over a list of Run objects, so the gh
plumbing is never exercised here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from scripts import ci_health_check as mod
from scripts import ci_health_trunk as trunk_mod

# EXCLUDED_RUN_EVENTS, the actions/runs page parse and the timestamp parse live in
# ci_health_core: both the checker (page 1) and the metrics catch-up (paged) read that
# listing, so the checker no longer owns them.
from scripts.ci_health_core import (
    EXCLUDED_RUN_EVENTS,
    EXIT_ITERATION_SPEED,
    EXIT_TRUNK_UNVERIFIED,
    Job,
    _parse_github_timestamp,
)

NOW = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)


def _run(
    *,
    age_minutes: int,
    duration_seconds: float,
    conclusion: str,
    branch: str = "main",
    event: str = "push",
    head_sha: str = "0" * 40,
) -> mod.Run:
    started = NOW - timedelta(minutes=age_minutes)
    return mod.Run(
        run_id=age_minutes,
        name="CI",
        event=event,
        head_branch=branch,
        head_sha=head_sha,
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
    assert "dynamic" in EXCLUDED_RUN_EVENTS


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
        _parse_github_timestamp(value, field, 123)
    assert field in str(excinfo.value)


def test_iteration_speed_is_the_least_severe_failure():
    """If iteration speed outranked billing then a slow build would mask an outage, or broken."""
    results = [
        mod.CheckResult("billing", False, "billing", "d", 8, mod.EXIT_BILLING),
        mod.CheckResult("iteration-speed", False, "iteration-speed", "d", 20, EXIT_ITERATION_SPEED),
    ]
    assert mod._resolve_exit_code(results) == mod.EXIT_BILLING
    assert EXIT_ITERATION_SPEED > mod.EXIT_STALENESS


# ----- R6 trunk-verified -------------------------------------------------------------
#
# These cover the two states that look identical downstream and are not: a trunk head
# whose jobs PASSED, and a trunk head whose jobs never ran. Both present as "not red".


def _job(name: str, conclusion: str):
    return Job(job_id=abs(hash(name)) % 10_000, name=name, conclusion=conclusion)


def _trunk_run(conclusion: str = "success"):
    return _run(age_minutes=1, duration_seconds=300, conclusion=conclusion)


def test_trunk_verified_passes_when_every_job_concluded_success(monkeypatch):
    """If a trunk head has all jobs green and this still alarms then it is crying wolf."""
    monkeypatch.setattr(
        trunk_mod,
        "fetch_run_jobs",
        lambda run_id: [_job("pytest", "success"), _job("ui", "success")],
    )
    result = mod._check_trunk_verified([_trunk_run()], "main")
    assert result.ok
    assert result.classification == "healthy"


def test_trunk_verified_treats_skipped_as_passing(monkeypatch):
    """If a path-filtered job counts as unverified then every docs-only merge alarms."""
    monkeypatch.setattr(
        trunk_mod,
        "fetch_run_jobs",
        lambda run_id: [_job("pytest", "success"), _job("deploy", "skipped")],
    )
    assert mod._check_trunk_verified([_trunk_run()], "main").ok


def test_trunk_verified_catches_jobs_that_never_executed(monkeypatch):
    """If a cancelled job reads as green then a merge burst silently leaves trunk unverified.

    This is the Mon 31 Aug 2026 case: the run itself did not fail, so nothing was red.
    """
    monkeypatch.setattr(
        trunk_mod,
        "fetch_run_jobs",
        lambda run_id: [_job("ratchet", "success"), _job("pytest fast lane", "cancelled")],
    )
    result = mod._check_trunk_verified([_trunk_run(conclusion="cancelled")], "main")
    assert not result.ok
    assert result.classification == "trunk-unverified"
    assert "pytest fast lane" in result.detail
    assert result.exit_code == EXIT_TRUNK_UNVERIFIED


def test_trunk_verified_catches_a_failed_job_inside_a_cancelled_run(monkeypatch):
    """If only the run conclusion is read then a real red hides inside a cancelled run.

    This is the 5f7466d3 case: the ratchet genuinely reported failure and went unseen for
    hours because the run's own top line said 'cancelled'.
    """
    monkeypatch.setattr(
        trunk_mod, "fetch_run_jobs", lambda run_id: [_job("quality ratchet", "failure")]
    )
    result = mod._check_trunk_verified([_trunk_run(conclusion="cancelled")], "main")
    assert not result.ok
    assert result.classification == "trunk-failed"
    assert "quality ratchet" in result.detail


def test_trunk_verified_alarms_when_the_gating_workflow_is_absent():
    """If a missing gating workflow passes quietly then the check cannot ever fail."""
    result = mod._check_trunk_verified([], "main")
    assert not result.ok
    assert result.classification == "insufficient-data"


def test_trunk_verified_ignores_pull_request_runs(monkeypatch):
    """If PR runs count as trunk then a green PR masks an unverified main."""
    monkeypatch.setattr(trunk_mod, "fetch_run_jobs", lambda run_id: [_job("pytest", "success")])
    pr_only = [
        _run(age_minutes=1, duration_seconds=300, conclusion="success", event="pull_request")
    ]
    assert mod._check_trunk_verified(pr_only, "main").classification == "insufficient-data"
