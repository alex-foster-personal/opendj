"""Cache accumulation tests for the cumulative CI spend ledger (#2020 / DEVOPS-09)."""

import json
from pathlib import Path

import pytest
import yaml

from scripts.ci_cost_ledger import RunRow, price_run
from scripts.ci_cost_model import Coverage
from tests.test_ci_cost_ledger import _job, _run

REPO = Path(__file__).resolve().parents[1]


def _sep_row(run_id: int) -> RunRow:
    day = f"2026-09-{1 + (run_id % 28):02d}"
    run = _run(run_id, "CI", "main", "success", f"{day}T00:00:00Z")
    return price_run(run, [_job(["ubuntu-latest"], f"{day}T00:00:00Z", f"{day}T00:10:00Z")])


def test_save_cache_merges_prior_rows_outside_the_listing_walk(tmp_path):
    """Replacing the cache with only this walk's rows erased every warm entry."""
    from scripts.ci_cost_ledger import load_cache, save_cache

    path = tmp_path / "cache.json"
    prior = {index: _sep_row(index) for index in range(1, 6)}
    save_cache(path, [_sep_row(99)], {"2026-09"}, prior=prior)

    assert set(load_cache(path)) == {1, 2, 3, 4, 5, 99}


def test_merge_report_rows_pulls_cached_rows_outside_the_listing(monkeypatch):
    """Observed Thu 10 Sep 2026: restore hit, yet the log still said 0 from cache."""
    from scripts import ci_cost_ledger

    cache = {index: _sep_row(index) for index in range(1, 401)}
    listing = [
        _run(index, "CI", "main", "success", "2026-09-11T00:00:00Z")
        for index in range(401, 501)
    ]
    monkeypatch.setattr(ci_cost_ledger, "fetch_runs", lambda *a, **k: (listing, 27409))
    monkeypatch.setattr(
        ci_cost_ledger,
        "fetch_jobs",
        lambda *a, **k: [_job(["ubuntu-latest"], "2026-09-11T00:00:00Z", "2026-09-11T00:01:00Z")],
    )

    walked, cov = ci_cost_ledger.build_ledger(
        "o/r", "2026-09-01", "token", 20, cache=cache, max_api_calls=400
    )
    merged = ci_cost_ledger.merge_report_rows(walked, cache, "2026-09")

    assert cov.priced - cov.api_calls == 0, "walk alone sees no overlap"
    assert len(merged) == 500
    assert len(merged) - cov.api_calls == 400


def test_two_run_month_to_date_used_minutes_is_monotonic(tmp_path, monkeypatch):
    from scripts import ci_cost_ledger

    path = tmp_path / "cache.json"
    runs_first = [
        _run(index, "CI", "main", "success", f"2026-09-{10 + index}T00:00:00Z")
        for index in range(1, 4)
    ]
    responses_first = [{"total_count": 3, "workflow_runs": runs_first}]
    responses_first += [
        {"jobs": [_job(["ubuntu-latest"], "2026-09-11T00:00:00Z", "2026-09-11T00:10:00Z")]}
        for _ in range(3)
    ]
    monkeypatch.setattr(ci_cost_ledger, "_get", lambda *a, **k: responses_first.pop(0))
    walked_first, _ = ci_cost_ledger.build_ledger(
        "o/r", "2026-09-01", "token", 20, cache={}, max_api_calls=400
    )
    merged_first = ci_cost_ledger.merge_report_rows(walked_first, {}, "2026-09")
    ci_cost_ledger.save_cache(path, walked_first, {"2026-09"}, prior={})
    used_first = sum(row.allowance_minutes for row in merged_first)

    cache = ci_cost_ledger.load_cache(path)
    runs_second = [_run(4, "CI", "main", "success", "2026-09-14T00:00:00Z"), *runs_first]
    responses_second = [{"total_count": 4, "workflow_runs": runs_second}]
    responses_second.append(
        {"jobs": [_job(["ubuntu-latest"], "2026-09-14T00:00:00Z", "2026-09-14T00:02:00Z")]}
    )
    monkeypatch.setattr(ci_cost_ledger, "_get", lambda *a, **k: responses_second.pop(0))
    walked_second, cov_second = ci_cost_ledger.build_ledger(
        "o/r", "2026-09-01", "token", 20, cache=cache, max_api_calls=400
    )
    merged_second = ci_cost_ledger.merge_report_rows(walked_second, cache, "2026-09")
    used_second = sum(row.allowance_minutes for row in merged_second)

    assert cov_second.api_calls == 1
    assert used_second >= used_first


def test_cache_gap_makes_coverage_incomplete_even_when_fully_priced():
    cov = Coverage(
        priced=100, listed=100, server_total=100, api_calls=0, api_budget_hit=False, cache_gap="missing"
    )
    assert cov.is_complete is False


def test_runs_json_past_month_without_rows_refuses_loudly(tmp_path, monkeypatch):
    from scripts import ci_cost_ledger

    fixture = tmp_path / "runs.json"
    fixture.write_text(
        json.dumps(
            {
                "runs": [_run(1, "CI", "main", "success", "2026-09-11T00:00:00Z")],
                "jobs": {
                    "1": [_job(["ubuntu-latest"], "2026-09-11T00:00:00Z", "2026-09-11T00:01:00Z")],
                },
            }
        ),
        encoding="utf-8",
    )
    report_file = tmp_path / "report.md"
    monkeypatch.setattr(
        "sys.argv",
        [
            "ci_cost_ledger",
            "--runs-json",
            str(fixture),
            "--month",
            "2026-08",
            "--report-file",
            str(report_file),
        ],
    )

    with pytest.raises(SystemExit, match="No priced runs for 2026-08"):
        ci_cost_ledger.main()
    assert not report_file.exists()


def test_budget_watch_uses_billing_usage_not_per_run_cache():
    workflow = yaml.safe_load((REPO / ".github" / "workflows" / "ci-budget-watch.yml").read_text())
    steps = [
        step
        for job in workflow.get("jobs", {}).values()
        for step in job.get("steps", [])
    ]
    cache_steps = [step for step in steps if step.get("with", {}).get("path") == "ci-cost-cache.json"]
    assert not cache_steps, "per-run priced-run cache was removed for billing usage API"
    ledger_step = next(step for step in steps if step.get("name") == "Build the spend ledger")
    run = ledger_step["run"]
    assert "--cache-file" not in run
    assert "--billing-user" in run or "${{ github.repository_owner }}" in run
