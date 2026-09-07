"""Tests for :mod:`scripts.ci_zero_step_triage`, the per-PR zero-step legibility tool.

Every case is a natural-language acceptance test from the module's mini-PRD (issue
#1166, hardened post-merge per the PR #1417 review). ``classify_job`` is pure over a
job dict shaped exactly like GitHub's Actions jobs API response, so no network or gh
subprocess is exercised in that section. The pagination and run-dedup sections stub
``_gh_api_json`` instead of the real ``gh`` subprocess, for the same reason.
"""

from __future__ import annotations

import pytest

from scripts.ci_health_core import EXIT_OK, REPO, PreconditionError
from scripts.ci_zero_step_triage import (
    LABEL_ACTION_REQUIRED,
    LABEL_CANCELLED,
    LABEL_INCOMPLETE,
    LABEL_NEUTRAL,
    LABEL_NO_RUNNER,
    LABEL_OK,
    LABEL_REAL_FAILURE,
    LABEL_SKIPPED,
    LABEL_STALE,
    LABEL_TIMED_OUT,
    _paginated_list,
    _run_ids_to_tally,
    _RunRef,
    _runs_at_head,
    classify_job,
    main,
)

# ----- classify_job: pure, one job dict at a time -----------------------------------


def test_failed_job_with_zero_steps_is_no_runner_not_a_defect() -> None:
    """if a job failed with an empty steps array then it is an infra refusal, not code."""
    job: dict[str, object] = {
        "name": "pytest fast lane (shard 1 of 5)",
        "conclusion": "failure",
        "steps": [],
    }

    label, steps, reason = classify_job(job)

    assert label == LABEL_NO_RUNNER
    assert steps == 0
    assert "not a code defect" in reason


def test_failed_job_missing_steps_field_is_also_no_runner() -> None:
    """if the jobs API omits steps entirely then it is treated as zero, never a guess."""
    job: dict[str, object] = {"name": "contracts", "conclusion": "failure"}

    label, steps, _reason = classify_job(job)

    assert label == LABEL_NO_RUNNER
    assert steps == 0


def test_failed_job_with_executed_steps_is_a_real_failure() -> None:
    """if a job ran real steps before failing then it counts in the failure tally."""
    job: dict[str, object] = {
        "name": "quality ratchet",
        "conclusion": "failure",
        "steps": [{"name": "checkout"}, {"name": "run quality gate"}],
    }

    label, steps, reason = classify_job(job)

    assert label == LABEL_REAL_FAILURE
    assert steps == 2
    assert "not a code defect" not in reason


def test_successful_job_is_ok_regardless_of_step_count() -> None:
    """if a job did not fail then it is OK, whatever its step count is."""
    job: dict[str, object] = {
        "name": "frontend",
        "conclusion": "success",
        "steps": [{"name": "build"}],
    }

    label, steps, _reason = classify_job(job)

    assert label == LABEL_OK
    assert steps == 1


def test_zero_step_skip_is_labelled_skipped_not_no_runner() -> None:
    """NO-RUNNER is specific to a failed job -- a skipped job with no steps is not
    miscast as an infrastructure refusal."""
    job: dict[str, object] = {"name": "docs-only skip", "conclusion": "skipped", "steps": []}

    label, _steps, _reason = classify_job(job)

    assert label == LABEL_SKIPPED
    assert label != LABEL_NO_RUNNER


def test_skipped_job_is_not_counted_as_ok() -> None:
    """P3 (PR #1420 review): a skipped job ran nothing, so it is not evidence
    anything passed -- it must not land in the same tally as success."""
    job: dict[str, object] = {
        "name": "pull_request-only gate",
        "conclusion": "skipped",
        "steps": [],
    }

    label, _steps, reason = classify_job(job)

    assert label == LABEL_SKIPPED
    assert label != LABEL_OK
    assert "not evidence" in reason


def test_malformed_steps_field_raises_instead_of_guessing() -> None:
    """if steps is present but not a list then this is a precondition failure, never 0."""
    job: dict[str, object] = {
        "id": 42,
        "name": "broken",
        "conclusion": "failure",
        "steps": "not-a-list",
    }

    with pytest.raises(PreconditionError):
        classify_job(job)


def test_timed_out_job_is_labelled_timed_out_not_ok() -> None:
    """if a job's conclusion is timed_out then it is named and counted with real failures.

    This is the regression case for the PR #1417 review finding: the pre-fix catch-all
    (``return LABEL_OK`` for anything that was not exactly ``"failure"``) mislabelled
    this OK. Restoring that catch-all is the mutation check for this guard.
    """
    job: dict[str, object] = {
        "name": "e2e smoke",
        "conclusion": "timed_out",
        "steps": [{"name": "run"}],
    }

    label, steps, reason = classify_job(job)

    assert label == LABEL_TIMED_OUT
    assert label != LABEL_OK
    assert steps == 1
    assert "real failures" in reason


def test_cancelled_job_is_excluded_not_ok() -> None:
    """if a job's conclusion is cancelled then it is named and excluded, never OK."""
    job: dict[str, object] = {"name": "quality ratchet", "conclusion": "cancelled", "steps": []}

    label, _steps, reason = classify_job(job)

    assert label == LABEL_CANCELLED
    assert label != LABEL_OK
    assert "excluded" in reason


def test_still_running_job_with_null_conclusion_is_incomplete_not_ok() -> None:
    """if a job is still queued or in progress (conclusion: null) then it is
    labelled INCOMPLETE, never defaulted to OK."""
    job: dict[str, object] = {
        "name": "pytest fast lane (shard 2 of 5)",
        "conclusion": None,
        "steps": [],
    }

    label, _steps, reason = classify_job(job)

    assert label == LABEL_INCOMPLETE
    assert label != LABEL_OK
    assert "excluded" in reason


@pytest.mark.parametrize(
    ("conclusion", "expected_label"),
    [
        ("action_required", LABEL_ACTION_REQUIRED),
        ("neutral", LABEL_NEUTRAL),
        ("stale", LABEL_STALE),
    ],
)
def test_other_github_conclusions_are_named_and_excluded_from_ok(
    conclusion: str, expected_label: str
) -> None:
    """every other GitHub-defined conclusion is named by itself, never OK."""
    job: dict[str, object] = {"name": "gate", "conclusion": conclusion, "steps": []}

    label, _steps, reason = classify_job(job)

    assert label == expected_label
    assert label != LABEL_OK
    assert "excluded" in reason


def test_unrecognized_conclusion_raises_instead_of_defaulting_to_ok() -> None:
    """if GitHub ever reports a conclusion this module does not enumerate, it fails
    loud rather than silently landing in the ok tally."""
    job: dict[str, object] = {
        "id": 7,
        "name": "gate",
        "conclusion": "some_future_value",
        "steps": [],
    }

    with pytest.raises(PreconditionError):
        classify_job(job)


# ----- pagination: _paginated_list stubs _gh_api_json, no real gh call -------------


def test_paginated_list_follows_every_page_past_the_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """if an endpoint's total_count exceeds one page then every page is read."""
    pages = {
        1: {"total_count": 3, "jobs": [{"n": 1}, {"n": 2}]},
        2: {"total_count": 3, "jobs": [{"n": 3}]},
    }

    def fake_gh_api_json(path: str) -> object:
        page_num = int(path.rsplit("page=", 1)[1])
        return pages[page_num]

    monkeypatch.setattr("scripts.ci_zero_step_triage._gh_api_json", fake_gh_api_json)

    items = _paginated_list("repos/x/y/actions/runs/1/jobs", "jobs")

    assert [item["n"] for item in items] == [1, 2, 3]


def test_paginated_list_raises_when_short_of_total_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """if paging stops before total_count is reached then it refuses rather than
    returning a silently truncated list."""

    def fake_gh_api_json(path: str) -> object:
        page_num = int(path.rsplit("page=", 1)[1])
        if page_num == 1:
            return {"total_count": 5, "jobs": [{"n": 1}]}
        return {"total_count": 5, "jobs": []}

    monkeypatch.setattr("scripts.ci_zero_step_triage._gh_api_json", fake_gh_api_json)

    with pytest.raises(PreconditionError):
        _paginated_list("repos/x/y/actions/runs/1/jobs", "jobs")


def test_paginated_list_raises_on_missing_total_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """if the endpoint response has no integer total_count then it refuses instead
    of guessing the list is complete."""

    def fake_gh_api_json(path: str) -> object:
        return {"jobs": [{"n": 1}]}

    monkeypatch.setattr("scripts.ci_zero_step_triage._gh_api_json", fake_gh_api_json)

    with pytest.raises(PreconditionError):
        _paginated_list("repos/x/y/actions/runs/1/jobs", "jobs")


# ----- run selection: pure over a list of _RunRef, and _runs_at_head's own guard ----


def test_run_ids_to_tally_keeps_every_run_never_just_the_latest_by_time() -> None:
    """P1 (PR #1420 review), BLOCKING: if the same workflow_id has two runs recorded
    at one head SHA -- an earlier one that is a REAL-FAILURE and a later one that is
    clean (e.g. the original pull_request run followed by an unrelated
    workflow_dispatch) -- then both run ids are tallied. Deduping to "newest run per
    workflow_id" silently discarded the earlier failure; this is the regression test
    for that bug.

    Mutation check performed by hand while fixing this: reverting
    ``_run_ids_to_tally`` to the old "keep only the newest run_id per workflow_id"
    behaviour turns this assertion red (kept == [101, 105], dropping the earlier,
    failing run 100 from workflow 1 while workflow 2's lone run 101 passes through
    unchanged), confirming the test actually exercises the fix before it was
    restored to the corrected body.
    """
    runs = [
        _RunRef(run_id=100, workflow_id=1),  # earlier run at this head: real failure
        _RunRef(run_id=105, workflow_id=1),  # later, unrelated trigger: clean
        _RunRef(run_id=101, workflow_id=2),
    ]

    kept = _run_ids_to_tally(runs)

    assert kept == [100, 101, 105]
    assert 100 in kept  # the earlier, failing run must not be dropped


def test_runs_at_head_rejects_a_non_dict_run_instead_of_raising_attributeerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P3 (PR #1420 review): a non-dict element in workflow_runs must fail with the
    module's own PreconditionError (the malformed-payload contract every other reader
    honours), never an AttributeError from calling .get() on a non-dict."""

    def fake_gh_api_json(path: str) -> object:
        return {"total_count": 1, "workflow_runs": ["not-a-dict"]}

    monkeypatch.setattr("scripts.ci_zero_step_triage._gh_api_json", fake_gh_api_json)

    with pytest.raises(PreconditionError):
        _runs_at_head("deadbeef")


# ----- main(): the tally branches, not just classify_job's return value ------------


def test_main_tallies_every_new_branch_correctly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """P3 (PR #1420 review): the skipped/excluded/timed_out branches in main() that map
    classify_job's labels onto the printed tally were untested -- every existing test
    asserted only on classify_job's own return value, so a future edit could route
    e.g. SKIPPED into the ok tally and the suite would stay green. Drives main() itself
    over one run with a job for every branch and asserts the printed summary line."""
    head_sha = "cafefeed"
    jobs: list[dict[str, object]] = [
        {"id": 1, "name": "ok job", "conclusion": "success", "steps": [{"n": 1}]},
        {"id": 2, "name": "no runner job", "conclusion": "failure", "steps": []},
        {"id": 3, "name": "real failure job", "conclusion": "failure", "steps": [{"n": 1}]},
        {"id": 4, "name": "timed out job", "conclusion": "timed_out", "steps": [{"n": 1}]},
        {"id": 5, "name": "skipped job", "conclusion": "skipped", "steps": []},
        {"id": 6, "name": "cancelled job", "conclusion": "cancelled", "steps": []},
        {"id": 7, "name": "action required job", "conclusion": "action_required", "steps": []},
    ]

    def fake_gh_api_json(path: str) -> object:
        if path.startswith(f"repos/{REPO}/pulls/42"):
            return {"head": {"sha": head_sha}}
        if path.startswith(f"repos/{REPO}/actions/runs?head_sha={head_sha}"):
            return {"total_count": 1, "workflow_runs": [{"id": 900, "workflow_id": 1}]}
        if path.startswith(f"repos/{REPO}/actions/runs/900/jobs"):
            return {"total_count": len(jobs), "jobs": jobs}
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr("scripts.ci_zero_step_triage._gh_api_json", fake_gh_api_json)

    exit_code = main(42)

    out = capsys.readouterr().out
    assert exit_code == EXIT_OK
    # real_failures = REAL-FAILURE (job 3) + TIMED-OUT (job 4); excluded = CANCELLED
    # (job 6) + ACTION-REQUIRED (job 7) -- neither is folded into ok or skipped.
    assert (
        "CI_ZERO_STEP_TRIAGE pr=42 head=cafefeed runs=1 no_runner=1 real_failures=2 "
        "ok=1 skipped=1 excluded=2" in out
    )
