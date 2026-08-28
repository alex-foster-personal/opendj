from scripts.ci_cost_guard import infer_standard_sku, price_jobs, render_markdown


def _job(name, labels, start, end, *, steps=None):
    return {
        "name": name,
        "labels": labels,
        "started_at": start,
        "completed_at": end,
        "conclusion": "success",
        "html_url": f"https://github.test/jobs/{name}",
        "steps": steps or [],
    }


def test_prices_parallel_jobs_by_rounded_job_minutes_not_workflow_wall_time():
    jobs = [
        _job("linux", ["ubuntu-latest"], "2026-01-01T00:00:00Z", "2026-01-01T00:01:01Z"),
        _job("windows", ["windows-latest"], "2026-01-01T00:00:00Z", "2026-01-01T00:01:01Z"),
        _job("mac", ["macos-latest"], "2026-01-01T00:00:00Z", "2026-01-01T00:01:01Z"),
    ]

    result = price_jobs(jobs)

    assert result["total_cost"] == (2 * 0.006) + (2 * 0.010) + (2 * 0.062)
    assert [row["billed_minutes"] for row in result["jobs"]] == [2, 2, 2]


def test_unknown_hosted_runner_is_unpriced_instead_of_assigned_a_cheap_rate():
    job = _job(
        "future-large-runner",
        ["custom-128-core"],
        "2026-01-01T00:00:00Z",
        "2026-01-01T00:02:00Z",
    )

    result = price_jobs([job])

    assert result["total_cost"] == 0
    assert result["jobs"] == []
    assert result["unknown_jobs"][0]["name"] == "future-large-runner"


def test_self_hosted_runner_has_zero_github_runner_charge():
    assert infer_standard_sku(["self-hosted", "linux"]).rate_usd_per_minute == 0


def test_started_subsecond_job_is_never_underpriced_as_zero_minutes():
    job = _job(
        "instant",
        ["ubuntu-latest"],
        "2026-01-01T00:00:00Z",
        "2026-01-01T00:00:00Z",
    )

    result = price_jobs([job])

    assert result["jobs"][0]["billed_minutes"] == 1
    assert result["total_cost"] == 0.006


def test_report_flags_strictly_greater_than_one_dollar_and_lists_steps():
    step = {
        "name": "test",
        "started_at": "2026-01-01T00:00:00Z",
        "completed_at": "2026-01-01T03:00:00Z",
        "conclusion": "failure",
    }
    job = _job(
        "runaway",
        ["ubuntu-latest"],
        "2026-01-01T00:00:00Z",
        "2026-01-01T03:00:00Z",
        steps=[step],
    )

    report, result = render_markdown(
        workflow_name="CI",
        run_url="https://github.test/runs/42",
        run_id="42",
        threshold=1.0,
        jobs=[job],
    )

    assert result["total_cost"] == 1.08
    assert result["over_threshold"] is True
    assert "**$1.080**" in report
    assert "runaway" in report
    assert "test" in report
