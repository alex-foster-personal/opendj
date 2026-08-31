"""Acceptance tests for the cumulative CI spend ledger.

Each test states the failure it prevents; the August 2026 outage happened
because nothing summed the allowance across runs.
"""

import json

import pytest

from scripts.ci_cost_ledger import (
    allowance_multiplier,
    group,
    month_to_date,
    price_run,
    render_report,
)


def _run(run_id, workflow, branch, conclusion, created, *, event="push", updated=None):
    return {
        "id": run_id,
        "name": workflow,
        "head_branch": branch,
        "event": event,
        "conclusion": conclusion,
        "created_at": created,
        "run_started_at": created,
        "updated_at": updated or created,
    }


def _job(labels, start, end, name="job"):
    return {
        "name": name,
        "labels": labels,
        "started_at": start,
        "completed_at": end,
        "conclusion": "success",
    }


# ----- allowance multipliers ---------------------------------------------


def test_macos_drains_ten_allowance_minutes_per_wall_minute():
    """If macOS is counted at 1x the budget looks fine until CI abruptly stops."""
    assert allowance_multiplier(["macos-14"]) == 10
    assert allowance_multiplier(["windows-latest"]) == 2
    assert allowance_multiplier(["ubuntu-latest"]) == 1


def test_self_hosted_drains_no_allowance():
    assert allowance_multiplier(["self-hosted", "linux"]) == 0


def test_unknown_runner_label_raises_instead_of_being_guessed_cheap():
    """A silently-cheap unknown runner is how a runaway stays invisible."""
    with pytest.raises(ValueError, match="refusing to guess"):
        allowance_multiplier(["gpu-96-core"])


# ----- pricing one run ----------------------------------------------------


def test_parallel_jobs_are_summed_not_taken_as_wall_time():
    """Three 1-minute parallel jobs cost 3 minutes, not the 1 minute of wall clock."""
    run = _run(1, "CI", "main", "success", "2026-08-31T00:00:00Z", updated="2026-08-31T00:01:00Z")
    jobs = [
        _job(["ubuntu-latest"], "2026-08-31T00:00:00Z", "2026-08-31T00:01:00Z", "a"),
        _job(["ubuntu-latest"], "2026-08-31T00:00:00Z", "2026-08-31T00:01:00Z", "b"),
        _job(["ubuntu-latest"], "2026-08-31T00:00:00Z", "2026-08-31T00:01:00Z", "c"),
    ]

    row = price_run(run, jobs)

    assert row.allowance_minutes == 3
    assert row.job_count == 3


def test_job_refused_a_runner_consumes_nothing():
    """The exhausted-allowance signature: no started_at, so it must price at zero."""
    run = _run(2, "CI", "main", "failure", "2026-08-31T14:04:16Z")
    jobs = [{"name": "test", "labels": ["ubuntu-latest"], "started_at": None, "completed_at": None}]

    row = price_run(run, jobs)

    assert row.allowance_minutes == 0
    assert row.job_count == 0


def test_macos_job_is_ten_times_a_linux_job_of_equal_length():
    run = _run(3, "macOS Packaging", "main", "success", "2026-08-31T00:00:00Z")
    row = price_run(run, [_job(["macos-14"], "2026-08-31T00:00:00Z", "2026-08-31T00:05:00Z")])

    assert row.allowance_minutes == 50
    assert row.cost_usd == pytest.approx(5 * 0.062)


def test_partial_minute_rounds_up_the_way_github_bills_it():
    run = _run(4, "CI", "main", "success", "2026-08-31T00:00:00Z")
    row = price_run(run, [_job(["ubuntu-latest"], "2026-08-31T00:00:00Z", "2026-08-31T00:01:01Z")])

    assert row.allowance_minutes == 2


def test_unpriced_runner_is_reported_not_silently_dropped():
    run = _run(5, "CI", "main", "success", "2026-08-31T00:00:00Z")
    row = price_run(
        run, [_job(["gpu-96-core"], "2026-08-31T00:00:00Z", "2026-08-31T00:01:00Z", "gpu")]
    )

    assert row.unpriced_jobs == ["gpu"]


# ----- aggregation and the budget gate ------------------------------------


def _month_of_runs(count, minutes_each, month="2026-08"):
    rows = []
    for index in range(count):
        run = _run(100 + index, "CI", "main", "success", f"{month}-15T00:00:00Z")
        jobs = [
            _job(
                ["ubuntu-latest"],
                f"{month}-15T00:00:00Z",
                f"{month}-15T00:{minutes_each:02d}:00Z",
            )
        ]
        rows.append(price_run(run, jobs))
    return rows


def test_many_individually_cheap_runs_still_trip_the_budget():
    """The exact August failure: no single run was expensive, the sum was."""
    rows = _month_of_runs(count=200, minutes_each=20)

    _, summary = render_report(
        rows,
        repository="o/r",
        month="2026-08",
        allowance_limit=3000,
        warn_pct=70,
        stop_pct=90,
        truncated=False,
    )

    assert summary["allowance_used"] == 4000
    assert summary["state"] == "STOP"


def test_state_is_ok_warn_stop_at_the_configured_thresholds():
    for minutes, expected in ((10, "OK"), (75, "WARN"), (95, "STOP")):
        rows = _month_of_runs(count=minutes, minutes_each=30)
        _, summary = render_report(
            rows,
            repository="o/r",
            month="2026-08",
            allowance_limit=3000,
            warn_pct=70,
            stop_pct=90,
            truncated=False,
        )
        assert summary["state"] == expected, f"{minutes} runs -> {summary}"


def test_previous_months_do_not_count_against_this_month():
    rows = _month_of_runs(count=200, minutes_each=20, month="2026-07")

    _, summary = render_report(
        rows,
        repository="o/r",
        month="2026-08",
        allowance_limit=3000,
        warn_pct=70,
        stop_pct=90,
        truncated=False,
    )

    assert summary["allowance_used"] == 0
    assert summary["state"] == "OK"


def test_cancelled_runs_are_counted_as_wasted_spend():
    """Cancel-in-progress churn is real spend; it must be visible, not hidden."""
    run = _run(9, "CI", "af--x", "cancelled", "2026-08-31T00:00:00Z")
    row = price_run(run, [_job(["ubuntu-latest"], "2026-08-31T00:00:00Z", "2026-08-31T00:03:00Z")])

    _, summary = render_report(
        [row],
        repository="o/r",
        month="2026-08",
        allowance_limit=3000,
        warn_pct=70,
        stop_pct=90,
        truncated=False,
    )

    assert row.is_waste
    assert summary["wasted_minutes"] == 3


def test_truncated_window_is_flagged_so_a_floor_is_not_read_as_a_total():
    rows = _month_of_runs(count=1, minutes_each=1)

    report, summary = render_report(
        rows,
        repository="o/r",
        month="2026-08",
        allowance_limit=3000,
        warn_pct=70,
        stop_pct=90,
        truncated=True,
    )

    assert summary["truncated"] is True
    assert "Window truncated" in report


def test_grouping_ranks_the_biggest_spender_first():
    cheap = price_run(
        _run(1, "Build docs", "main", "success", "2026-08-31T00:00:00Z"),
        [_job(["ubuntu-latest"], "2026-08-31T00:00:00Z", "2026-08-31T00:01:00Z")],
    )
    dear = price_run(
        _run(2, "CI", "main", "success", "2026-08-31T00:00:00Z"),
        [_job(["ubuntu-latest"], "2026-08-31T00:00:00Z", "2026-08-31T00:30:00Z")],
    )

    assert [name for name, _ in group([cheap, dear], "workflow")] == ["CI", "Build docs"]


def test_month_to_date_filters_by_created_at_prefix():
    rows = _month_of_runs(count=2, minutes_each=1, month="2026-08")
    rows += _month_of_runs(count=3, minutes_each=1, month="2026-07")

    assert len(month_to_date(rows, "2026-08")) == 2


# ----- offline CLI --------------------------------------------------------


def test_cli_prices_an_offline_fixture_without_network(tmp_path, monkeypatch, capsys):
    from scripts import ci_cost_ledger

    fixture = tmp_path / "runs.json"
    fixture.write_text(
        json.dumps(
            {
                "runs": [_run(1, "CI", "main", "success", "2026-08-31T00:00:00Z")],
                "jobs": {
                    "1": [
                        _job(
                            ["macos-14"],
                            "2026-08-31T00:00:00Z",
                            "2026-08-31T00:10:00Z",
                        )
                    ]
                },
            }
        )
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
            "--allowance-minutes",
            "3000",
            "--report-file",
            str(report_file),
        ],
    )

    assert ci_cost_ledger.main() == 0
    assert "100 / 3000 min" in report_file.read_text()


def test_cli_exits_nonzero_when_over_the_stop_threshold(tmp_path, monkeypatch):
    """The gate must fail the job, or a scheduled watcher reports green while burning."""
    from scripts import ci_cost_ledger

    fixture = tmp_path / "runs.json"
    fixture.write_text(
        json.dumps(
            {
                "runs": [_run(1, "CI", "main", "success", "2026-08-31T00:00:00Z")],
                "jobs": {
                    "1": [
                        _job(
                            ["ubuntu-latest"],
                            "2026-08-31T00:00:00Z",
                            "2026-08-31T02:00:00Z",
                        )
                    ]
                },
            }
        )
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "ci_cost_ledger",
            "--runs-json",
            str(fixture),
            "--month",
            "2026-08",
            "--allowance-minutes",
            "100",
            "--report-file",
            str(tmp_path / "r.md"),
            "--fail-on-stop",
        ],
    )

    assert ci_cost_ledger.main() == 1


# ----- incremental cache --------------------------------------------------


def _row(run_id, conclusion="success", created="2026-08-31T00:00:00Z"):
    return price_run(
        _run(run_id, "CI", "main", conclusion, created),
        [_job(["ubuntu-latest"], created, created.replace("T00:00", "T00:05"))],
    )


def test_finished_runs_are_cacheable_but_running_ones_are_not():
    """Caching an in-progress run would freeze it at a partial cost forever."""
    assert _row(1, "success").is_terminal
    assert _row(2, "cancelled").is_terminal
    assert not _row(3, None).is_terminal


def test_cache_round_trips_a_row_without_losing_its_cost(tmp_path):
    from scripts.ci_cost_ledger import load_cache, save_cache

    path = tmp_path / "cache.json"
    original = _row(7)
    save_cache(path, [original], {"2026-08"})

    restored = load_cache(path)[7]

    assert restored.allowance_minutes == original.allowance_minutes
    assert restored.conclusion == original.conclusion


def test_cache_drops_months_outside_the_retention_window(tmp_path):
    """Unbounded growth would eventually make the cache artifact the problem."""
    from scripts.ci_cost_ledger import load_cache, save_cache

    path = tmp_path / "cache.json"
    save_cache(
        path,
        [_row(1, created="2026-08-01T00:00:00Z"), _row(2, created="2026-05-01T00:00:00Z")],
        {"2026-08", "2026-07"},
    )

    assert set(load_cache(path)) == {1}


def test_build_ledger_skips_the_api_for_cached_finished_runs(monkeypatch):
    """The point of the cache: 1,000 GITHUB_TOKEN calls/hr is a real ceiling."""
    from scripts import ci_cost_ledger

    runs = [
        _run(1, "CI", "main", "success", "2026-08-31T00:00:00Z"),
        _run(2, "CI", "main", "success", "2026-08-31T01:00:00Z"),
    ]
    monkeypatch.setattr(ci_cost_ledger, "fetch_runs", lambda *a, **k: runs)
    calls = []

    def _fetch_jobs(repository, run_id, token):
        calls.append(run_id)
        return [_job(["ubuntu-latest"], "2026-08-31T00:00:00Z", "2026-08-31T00:05:00Z")]

    monkeypatch.setattr(ci_cost_ledger, "fetch_jobs", _fetch_jobs)

    rows, _, fetched = ci_cost_ledger.build_ledger(
        "o/r", "2026-08-01", "token", 20, cache={1: _row(1)}
    )

    assert calls == [2], "run 1 was cached and must not be re-fetched"
    assert fetched == 1
    assert len(rows) == 2


def test_cached_row_that_was_still_running_is_repriced(monkeypatch):
    from scripts import ci_cost_ledger

    runs = [_run(1, "CI", "main", "success", "2026-08-31T00:00:00Z")]
    monkeypatch.setattr(ci_cost_ledger, "fetch_runs", lambda *a, **k: runs)
    monkeypatch.setattr(
        ci_cost_ledger,
        "fetch_jobs",
        lambda *a, **k: [_job(["ubuntu-latest"], "2026-08-31T00:00:00Z", "2026-08-31T00:09:00Z")],
    )

    rows, _, fetched = ci_cost_ledger.build_ledger(
        "o/r", "2026-08-01", "token", 20, cache={1: _row(1, conclusion=None)}
    )

    assert fetched == 1
    assert rows[0].allowance_minutes == 9
