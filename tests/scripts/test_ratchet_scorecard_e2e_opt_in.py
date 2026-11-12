"""Issue #11491: scorecard full verdict tracks e2e opt-in and deliberate job skips."""

from __future__ import annotations

import datetime as dt

import pytest

from scripts import ratchet_scorecard as rs

T0 = dt.datetime(2026, 11, 8, 12, 0, tzinfo=dt.UTC)


def _ci(
    branch: str,
    sha: str,
    *,
    measured: bool = True,
    e2e_required: bool = True,
    skipped: frozenset[str] | None = None,
    aggregate: str = rs.AGGREGATE_SUCCESS,
    conclusion: str = "success",
) -> rs.Head:
    return rs.Head(
        "ci.yml",
        branch,
        sha,
        T0,
        frozenset(),
        measured,
        skipped=skipped or frozenset(),
        aggregate=aggregate,
        conclusion=conclusion,
        e2e_required=e2e_required,
    )


@pytest.mark.requirement("DEVOPS-10534")
def test_an_unlabelled_head_with_ci_only_counts_as_a_full_verdict() -> None:
    """[if] no e2e label and no e2e run [then] ci.yml alone is scored, [else stop]."""
    window = (T0, T0 + dt.timedelta(days=1))
    head = _ci("b", "s1", e2e_required=False)
    runs = {"ci.yml": head}
    assert rs.full_verdict(runs) is True
    assert rs.required_workflows(runs) == frozenset({"ci.yml"})
    assert rs.incomplete_heads([head], window) == 0
    assert len(rs.owned_shas([head], window)) == 1
    # Mutation: old rule requiring both workflows -- `not (...)` becomes False if reverted.
    assert not (set(rs.WORKFLOWS) <= runs.keys())


@pytest.mark.requirement("DEVOPS-10534")
def test_a_job_skipped_by_its_own_if_is_not_a_missing_job_reason() -> None:
    """[if] aggregate success skipped a gated job [then] missing_job is not reported, [else stop]."""
    skipped = frozenset({"production frontend build"})
    head = _ci(
        "b",
        "s2",
        measured=False,
        skipped=skipped,
        aggregate=rs.AGGREGATE_SUCCESS,
        e2e_required=True,
    )
    assert rs.workflow_verdict_satisfied(head) is True
    counts, _ = rs.unread_head_reasons([head], (T0, T0 + dt.timedelta(days=1)))
    assert counts.get("missing_job", 0) == 0
    # Mutation: treat every unmeasured success run as missing_job -- fails above.
    assert not head.measured and head.conclusion == "success"


@pytest.mark.requirement("DEVOPS-10534")
def test_a_labelled_head_with_no_e2e_run_stays_incomplete() -> None:
    """[if] e2e label present but no e2e.yml run [then] verdict stays UNKNOWN, [else stop]."""
    runs = {"ci.yml": _ci("b", "s3", e2e_required=True)}
    assert rs.full_verdict(runs) is False
    assert rs.required_workflows(runs) == frozenset(rs.WORKFLOWS)
    counts, samples = rs.unread_head_reasons([runs["ci.yml"]], (T0, T0 + dt.timedelta(days=1)))
    assert counts.get("missing_workflow") == 1
    assert samples["missing_workflow"]["detail"].startswith("no e2e.yml run")
    # Mutation: drop e2e from required set when only ci is present -- full_verdict becomes True.
    assert "e2e.yml" in rs.required_workflows(runs)
