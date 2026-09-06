"""Tests for :mod:`scripts.ci_zero_step_triage`, the per-PR zero-step legibility tool.

Every case is a natural-language acceptance test from the module's mini-PRD (issue
#1166). ``classify_job`` is pure over a job dict shaped exactly like GitHub's Actions
jobs API response, so no network or gh subprocess is exercised here.
"""

from __future__ import annotations

import pytest

from scripts.ci_health_core import PreconditionError
from scripts.ci_zero_step_triage import (
    LABEL_NO_RUNNER,
    LABEL_OK,
    LABEL_REAL_FAILURE,
    classify_job,
)


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


def test_zero_step_success_is_ok_not_no_runner() -> None:
    """NO-RUNNER is specific to a failed job -- a skipped/success job with no
    steps is not miscast as an infrastructure refusal."""
    job: dict[str, object] = {"name": "docs-only skip", "conclusion": "skipped", "steps": []}

    label, _steps, _reason = classify_job(job)

    assert label == LABEL_OK


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
