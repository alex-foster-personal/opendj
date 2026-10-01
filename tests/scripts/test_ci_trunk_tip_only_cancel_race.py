"""Tests for the completed-run cancel race in :mod:`scripts.ci_trunk_tip_only`.

trunk-tip-only run 36865249326 (and 36862501942) at main 77b54cbe2004 failed exit 10
because a queued run (36803336485) finished between the listing and the cancel POST,
which GitHub refuses with HTTP 409. The outcome the sweep wanted -- that run not
running -- already held; failing loud on it made trunk read red when it was not.

GitHub does not use one fixed wording for that 409: live evidence from the two failed
sweeps carries "Cannot cancel a workflow run that is completed" on one run and "Cannot
cancel a workflow run that is not in progress" on the SAME run id the next sweep. A
check that matched only the first phrase would miss the second, so `_cancel_run`
re-reads the run's own `status` after a refused cancel and trusts that over the error
text -- the presence check, not an absence-of-failure guess.

Regression lines:
  - if a completed-run 409 cancel failure is not counted as success then broken
  - if a 403 or 500 cancel failure is swallowed instead of raised then broken
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from scripts import ci_trunk_tip_only as mod
from scripts.ci_health_core import PreconditionError

TRUNK_TIP = "a" * 40
OTHER_SHA = "b" * 40


def _run(run_id: int, *, name: str = "Stable evidence") -> mod.QueuedRun:
    return mod.QueuedRun(
        run_id=run_id,
        name=name,
        event="push",
        head_branch="main",
        head_repo_owner="maintainer",
        head_sha=OTHER_SHA,
        created_at=datetime(2026, 10, 1, 12, 33, 0, tzinfo=UTC),
    )


def _raising_run_gh(message: str):
    def _fake(_args: list[str]) -> str:
        raise PreconditionError(
            f"gh api --method POST repos/example/actions/runs/1/cancel failed with exit 1: {message}"
        )

    return _fake


def _status_reader(asked: list[str], status: object):
    def _fake(path: str) -> object:
        asked.append(path)
        return {"status": status}

    return _fake


# ----- presence check: re-read status wins over a guessed phrase -----


def test_a_completed_run_409_counts_as_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact wording from run 36865249326's failure, confirmed completed on re-read."""
    monkeypatch.setattr(
        mod, "_run_gh", _raising_run_gh("gh: Cannot cancel a workflow run that is completed. (HTTP 409)")
    )
    monkeypatch.setattr(mod, "_gh_api_json", _status_reader([], "completed"))
    assert mod._cancel_run(1) is mod.CancelOutcome.ALREADY_COMPLETED


def test_a_differently_worded_completed_409_also_counts_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wording from run 36862501942's failure on the SAME run id. A fixed phrase match
    tuned to the first wording would miss this one; the status re-read does not care which
    words GitHub used."""
    monkeypatch.setattr(
        mod,
        "_run_gh",
        _raising_run_gh("gh: Cannot cancel a workflow run that is not in progress. (HTTP 409)"),
    )
    monkeypatch.setattr(mod, "_gh_api_json", _status_reader([], "completed"))
    assert mod._cancel_run(1) is mod.CancelOutcome.ALREADY_COMPLETED


def test_the_recheck_asks_about_the_cancelled_runs_own_id(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The control: the re-read must be scoped to THIS run, or it answers a different
    question than the one the failed cancel raised. Also the only place this race is
    logged: `_cancel_run` prints the notice itself, so every caller gets it for free."""
    asked: list[str] = []
    monkeypatch.setattr(
        mod, "_run_gh", _raising_run_gh("gh: Cannot cancel a workflow run that is completed. (HTTP 409)")
    )
    monkeypatch.setattr(mod, "_gh_api_json", _status_reader(asked, "completed"))
    mod._cancel_run(36803336485)
    assert asked == [f"repos/{mod.REPO}/actions/runs/36803336485"]
    out = capsys.readouterr().out
    assert "cancel-already-completed run_id=36803336485 reason=already-completed" in out


# ----- the opposite direction: a genuine failure must still raise -----


def test_a_403_with_the_run_still_queued_still_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation-the-other-way control: if the recheck is ever widened to "any cancel error
    is fine", this must go red. A permissions failure on a run that has NOT completed is a
    real failure, not the race this fix tolerates."""
    monkeypatch.setattr(mod, "_run_gh", _raising_run_gh("gh: Resource not accessible (HTTP 403)"))
    monkeypatch.setattr(mod, "_gh_api_json", _status_reader([], "queued"))
    with pytest.raises(PreconditionError):
        mod._cancel_run(1)


def test_a_500_with_the_run_still_in_progress_still_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "_run_gh", _raising_run_gh("gh: Internal Server Error (HTTP 500)"))
    monkeypatch.setattr(mod, "_gh_api_json", _status_reader([], "in_progress"))
    with pytest.raises(PreconditionError):
        mod._cancel_run(1)


def test_a_malformed_status_rereard_raises_rather_than_a_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read that cannot measure must not render as a result in either direction."""
    monkeypatch.setattr(mod, "_run_gh", _raising_run_gh("gh: Conflict (HTTP 409)"))
    monkeypatch.setattr(mod, "_gh_api_json", lambda _path: {"message": "Not Found"})
    with pytest.raises(PreconditionError):
        mod._cancel_run(1)


def test_not_yet_queued_is_still_resolved_without_a_status_rereard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control against overreach in the other function: the pre-existing not-yet-queued
    409 path must not start paying for a status re-read it never needed."""
    asked: list[str] = []
    monkeypatch.setattr(
        mod,
        "_run_gh",
        _raising_run_gh("gh: Cannot cancel a workflow run that has not been queued yet. (HTTP 409)"),
    )
    monkeypatch.setattr(mod, "_gh_api_json", _status_reader(asked, "completed"))
    assert mod._cancel_run(1) is mod.CancelOutcome.SKIPPED_NOT_YET_QUEUED
    assert asked == []


# ----- the three call sites all treat already-completed as achieved, not failed -----


def test_execute_sweep_counts_an_already_completed_bookkeeping_cancel_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run(36803336485)
    plan = mod.SweepPlan(
        trunk_tip=TRUNK_TIP,
        retained_head_shas=frozenset({TRUNK_TIP}),
        retained_ci_run_ids=frozenset(),
        ci_to_cancel=(),
        bookkeeping_to_cancel=(run,),
        bookkeeping_kept=0,
    )
    monkeypatch.setattr(mod, "_cancel_run", lambda _run_id: mod.CancelOutcome.ALREADY_COMPLETED)
    report = mod.execute_sweep(plan, dry_run=False)
    assert report.bookkeeping_cancelled == 1


def test_execute_sweep_counts_an_already_completed_ci_cancel_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run(36803336485, name="CI")
    plan = mod.SweepPlan(
        trunk_tip=TRUNK_TIP,
        retained_head_shas=frozenset({TRUNK_TIP}),
        retained_ci_run_ids=frozenset(),
        ci_to_cancel=(run,),
        bookkeeping_to_cancel=(),
        bookkeeping_kept=0,
    )
    monkeypatch.setattr(mod, "_cancel_run", lambda _run_id: mod.CancelOutcome.ALREADY_COMPLETED)
    report = mod.execute_sweep(plan, dry_run=False)
    assert report.ci_cancelled == 1


def test_closed_pr_sweep_counts_an_already_completed_cancel_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This is the exact call site that raised in both live failures: closed-pr-cancel
    reason=no-open-pr on run 36803336485."""
    run = mod.QueuedRun(
        run_id=36803336485,
        name="CI",
        event="pull_request",
        head_branch="af--libm120-fill-pages",
        head_repo_owner="maintainer",
        head_sha=OTHER_SHA,
        created_at=datetime(2026, 10, 1, 1, 54, 46, tzinfo=UTC),
    )
    monkeypatch.setattr(mod, "_cancel_run", lambda _run_id: mod.CancelOutcome.ALREADY_COMPLETED)
    counts = mod.execute_closed_pr_sweep((run,), dry_run=False, still_closed=lambda _: True)
    assert (counts.planned, counts.cancelled) == (1, 1)


def test_closed_pr_sweep_still_propagates_a_genuine_cancel_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control: this call site must not have quietly started swallowing every cancel
    error along with the completed-run race."""
    run = mod.QueuedRun(
        run_id=1,
        name="CI",
        event="pull_request",
        head_branch="af--merged",
        head_repo_owner="maintainer",
        head_sha=OTHER_SHA,
        created_at=datetime(2026, 10, 1, 1, 54, 46, tzinfo=UTC),
    )

    def _raise(_run_id: int) -> mod.CancelOutcome:
        raise PreconditionError("gh api cancel failed with exit 1: gh: Resource not accessible (HTTP 403)")

    monkeypatch.setattr(mod, "_cancel_run", _raise)
    with pytest.raises(PreconditionError):
        mod.execute_closed_pr_sweep((run,), dry_run=False, still_closed=lambda _: True)
