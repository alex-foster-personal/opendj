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
from scripts.ci_health_core import EXIT_ITERATION_SPEED

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


def test_iteration_speed_is_the_least_severe_failure():
    """If iteration speed outranked billing then a slow build would mask an outage, or broken."""
    results = [
        mod.CheckResult("billing", False, "billing", "d", 8, mod.EXIT_BILLING),
        mod.CheckResult("iteration-speed", False, "iteration-speed", "d", 20, EXIT_ITERATION_SPEED),
    ]
    assert mod._resolve_exit_code(results) == mod.EXIT_BILLING
    assert EXIT_ITERATION_SPEED > mod.EXIT_STALENESS
