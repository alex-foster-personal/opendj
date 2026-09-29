"""The cost guard's pricing, and the workflow readers it is checked with.

Everything here runs on hand-written input: synthetic job payloads shaped like
the ones the GitHub API returns, and `runs-on` / `if` expressions written out
in the test. Nothing reads `.github/workflows`, so a failure names a defect in
the arithmetic or the parser rather than a workflow someone edited. The
assertions that DO run over the workflows on disk live in
tests/test_ci_cost_guard_workflow_coverage.py.
"""

from __future__ import annotations

import json

import pytest

from scripts.ci_cost_guard import infer_standard_sku, price_jobs, render_markdown
from tests.ci_cost_guard_workflow_reader import (
    CANARY_CONFIG,
    event_set,
    matrix_runs,
    runner_labels,
)
from tests.scripts.ci_runner_routes import CANARY_RUNS_ON, CANARY_VENDOR_MATRIX


def _canary_vendors() -> dict:
    return json.loads(CANARY_CONFIG.read_text())["vendors"]


# ----- what the guard charges for a run it is handed ------------------------


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


# ----- how this suite reads a `runs-on` it was not given literally ----------


def test_a_runner_switch_is_priced_at_the_hosted_fallback_it_can_select() -> None:
    """The ceiling has to survive the variable being unset, which is hosted.

    Self-hosted is $0 in the SKU table, so resolving the switch the other way
    would silently zero every Linux ceiling on disk and drop workflows off the
    watch list without a single assertion going red - the exact fail-open this
    module exists to prevent.
    """
    switch = "${{ fromJSON(vars.CI_RUNS_ON_LINUX || '\"ubuntu-latest\"') }}"

    assert runner_labels("test", switch) == ["ubuntu-latest"]
    sku = infer_standard_sku(runner_labels("test", switch))
    assert sku is not None, "the hosted fallback must price through the SKU table"
    assert sku.rate_usd_per_minute > 0


def test_a_chained_runner_switch_is_priced_at_the_final_hosted_fallback() -> None:
    """The CI test job chains pytest/e2e/linux vars before ubuntu-latest."""
    switch = (
        "${{ fromJSON(vars.CI_RUNS_ON_PYTEST || vars.CI_RUNS_ON_E2E "
        "|| vars.CI_RUNS_ON_LINUX || '\"ubuntu-latest\"') }}"
    )

    assert runner_labels("test", switch) == ["ubuntu-latest"]


def test_a_chained_runner_switch_with_an_unreadable_tail_is_refused() -> None:
    """Fail-closed: only vars.* disjuncts before the JSON literal are priced."""
    unreadable = "${{ fromJSON(vars.CI_RUNS_ON_LINUX || inputs.runner || '\"ubuntu-latest\"') }}"
    with pytest.raises(AssertionError, match="cannot"):
        runner_labels("test", unreadable)


def test_a_runner_switch_falling_back_to_a_label_list_keeps_every_label() -> None:
    """The fallback is JSON, and GitHub accepts a list there as well as a string."""
    switch = '${{ fromJSON(vars.CI_RUNS_ON_E2E || \'["macos-latest","large"]\') }}'

    assert runner_labels("gate", switch) == ["macos-latest", "large"]


def test_a_runs_on_expression_this_test_cannot_read_is_refused_not_guessed() -> None:
    """Fail-closed, like `event_set`: an unpriced runner must redden, not vanish.

    A `runs-on` resolved by guesswork could name self-hosted ($0) for a job
    that really runs on macOS, and the workflow would leave the watch list with
    every assertion still green.
    """
    for unreadable in (
        "${{ vars.CI_RUNS_ON_LINUX }}",
        "${{ fromJSON(inputs.runner) }}",
        "${{ fromJSON(vars.CI_RUNS_ON_LINUX) }}",
    ):
        with pytest.raises(AssertionError, match="cannot"):
            runner_labels("test", unreadable)


def test_the_runner_canary_label_map_prices_as_its_configured_vendor_labels() -> None:
    """if the canary's runs-on stops resolving to self-hosted vendor labels then broken

    ADR-NEW-runner-canary: third-party runners register as self-hosted, so GitHub bills
    $0 for them. The widening is an EXACT match, so any fallback spliced into the
    expression is unreadable again and refuses rather than pricing as free.
    """
    labels = runner_labels("pytest", CANARY_RUNS_ON)
    assert labels[0] == "self-hosted"
    assert set(labels[1:]) == {v["label"] for v in _canary_vendors().values()}
    assert infer_standard_sku(labels).rate_usd_per_minute == 0
    with_fallback = CANARY_RUNS_ON.replace(" }}", " || 'macos-latest' }}")
    with pytest.raises(AssertionError, match="cannot"):
        runner_labels("pytest", with_fallback)


def test_the_runner_canary_matrix_counts_every_configured_vendor() -> None:
    """if the vendor matrix expression prices as one run, or refuses, then broken"""
    job = {"strategy": {"matrix": {"vendor": CANARY_VENDOR_MATRIX, "shard": [1, 2, 3, 4, 5]}}}
    assert matrix_runs("pytest", job) == 5 * len(_canary_vendors())
    other = {"strategy": {"matrix": {"vendor": "${{ fromJSON(inputs.vendors) }}"}}}
    with pytest.raises(AssertionError, match="not a literal list"):
        matrix_runs("pytest", other)


def test_a_literal_runs_on_is_still_read_exactly_as_written() -> None:
    """CONTROL: the resolver must not start rewriting runners that need no help."""
    assert runner_labels("test", "ubuntu-latest") == ["ubuntu-latest"]
    assert runner_labels("test", ["self-hosted", "linux"]) == ["self-hosted", "linux"]


# ----- how this suite reads an `if` gate ------------------------------------


def test_a_status_guarded_event_gate_is_read_and_any_other_shape_still_refuses() -> None:
    """if the widening leaks then an unreadable clause prices as a small event set"""
    guarded = (
        "!cancelled() && (github.event_name == 'schedule' "
        "|| github.event_name == 'workflow_dispatch')"
    )
    assert event_set(guarded, "github.event_name") == {"schedule", "workflow_dispatch"}

    # The widening must not have become "skip whatever you cannot read". A ref
    # comparison admits every event the workflow triggers on, and returning a
    # small set for it is the exact fail-open this parser exists to prevent.
    with pytest.raises(AssertionError, match="cannot compute the event set"):
        event_set("github.ref == 'refs/heads/main'", "github.event_name")

    # And a status guard around something unreadable is still unreadable.
    with pytest.raises(AssertionError, match="cannot compute the event set"):
        event_set("!cancelled() && (github.ref == 'refs/heads/main')", "github.event_name")
