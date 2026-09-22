"""Tests for :mod:`scripts.ci_trunk_tip_only`.

Regression lines:
  - if a superseded Stable evidence run stays queued after sweep then broken
  - if dry_run performs a cancel POST then broken
  - if retained CI push runs are not exactly oldest and newest then broken
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from scripts import ci_trunk_tip_only as mod
from scripts.ci_health_core import PreconditionError

TRUNK_TIP = "a" * 40
OTHER_SHA = "b" * 40
MIDDLE_SHA = "c" * 40
OLDEST_SHA = "d" * 40
NEWEST_SHA = "e" * 40

BASE_TIME = datetime(2026, 9, 15, 7, 0, 0, tzinfo=UTC)


def _run(
    run_id: int,
    *,
    name: str,
    event: str = "push",
    head_sha: str = OTHER_SHA,
    minutes: int = 0,
) -> mod.QueuedRun:
    return mod.QueuedRun(
        run_id=run_id,
        name=name,
        event=event,
        head_branch="main",
        head_repo_owner="maintainer",
        head_sha=head_sha,
        created_at=BASE_TIME.replace(minute=minutes % 60),
    )


def test_retained_ci_push_runs_keep_oldest_and_newest_only() -> None:
    """If four queued CI push runs exist then only the oldest and newest are retained."""
    runs = [
        _run(1, name="CI", head_sha=OLDEST_SHA, minutes=0),
        _run(2, name="CI", head_sha=MIDDLE_SHA, minutes=10),
        _run(3, name="CI", head_sha=OTHER_SHA, minutes=20),
        _run(4, name="CI", head_sha=NEWEST_SHA, minutes=30),
    ]
    plan = mod.build_sweep_plan(TRUNK_TIP, runs)
    assert plan.retained_ci_run_ids == frozenset({1, 4})
    assert plan.ci_to_cancel == (runs[1], runs[2])
    assert TRUNK_TIP in plan.retained_head_shas
    assert OLDEST_SHA in plan.retained_head_shas
    assert NEWEST_SHA in plan.retained_head_shas
    assert MIDDLE_SHA not in plan.retained_head_shas


def test_bookkeeping_for_superseded_sha_is_cancelled() -> None:
    """If bookkeeping head_sha is not retained then it is scheduled for cancellation."""
    runs = [
        _run(10, name="Stable evidence", head_sha=OTHER_SHA),
        _run(11, name="Error sink", head_sha=TRUNK_TIP),
        _run(12, name="Error sink", head_sha=OTHER_SHA),
    ]
    plan = mod.build_sweep_plan(TRUNK_TIP, runs)
    assert plan.bookkeeping_to_cancel == (runs[0], runs[2])
    assert plan.bookkeeping_kept == 1


def test_bookkeeping_for_retained_sha_is_kept() -> None:
    """If bookkeeping head_sha matches trunk tip then it is kept."""
    runs = [_run(20, name="Stable evidence", head_sha=TRUNK_TIP)]
    plan = mod.build_sweep_plan(TRUNK_TIP, runs)
    assert plan.bookkeeping_to_cancel == ()
    assert plan.bookkeeping_kept == 1


def test_execute_sweep_logs_superseded_by_and_skips_post_in_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dry-run emits superseded_by logs and never POSTs cancellations."""
    run = _run(30, name="Stable evidence", head_sha=OTHER_SHA)
    plan = mod.SweepPlan(
        trunk_tip=TRUNK_TIP,
        retained_head_shas=frozenset({TRUNK_TIP}),
        retained_ci_run_ids=frozenset(),
        ci_to_cancel=(),
        bookkeeping_to_cancel=(run,),
        bookkeeping_kept=0,
    )
    posts: list[int] = []

    def _fake_cancel(run_id: int) -> None:
        posts.append(run_id)

    monkeypatch.setattr(mod, "_cancel_run", _fake_cancel)
    report = mod.execute_sweep(plan, dry_run=True)
    assert report.bookkeeping_cancelled == 1
    assert posts == []


def test_execute_sweep_posts_cancel_when_not_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """A live sweep POSTs cancel for each selected bookkeeping run."""
    run = _run(31, name="Error sink", head_sha=OTHER_SHA)
    plan = mod.SweepPlan(
        trunk_tip=TRUNK_TIP,
        retained_head_shas=frozenset({TRUNK_TIP}),
        retained_ci_run_ids=frozenset(),
        ci_to_cancel=(),
        bookkeeping_to_cancel=(run,),
        bookkeeping_kept=0,
    )
    posts: list[int] = []

    def _fake_cancel(run_id: int) -> mod.CancelOutcome:
        posts.append(run_id)
        return mod.CancelOutcome.CANCELLED

    monkeypatch.setattr(mod, "_cancel_run", _fake_cancel)
    report = mod.execute_sweep(plan, dry_run=False)
    assert posts == [31]
    assert report.bookkeeping_cancelled == 1
    assert report.bookkeeping_cancel_skipped_not_yet_queued == 0


def test_cancel_log_line_includes_superseded_by() -> None:
    """Cancellation logs name the superseding trunk tip SHA."""
    run = _run(32, name="Error sink", head_sha=OTHER_SHA)
    line = mod._cancel_log_line(run, TRUNK_TIP)
    assert "superseded_by=" + TRUNK_TIP in line
    assert "workflow=Error sink" in line
    assert f"run_id={run.run_id}" in line


def test_cancel_run_tolerates_not_yet_queued_409(monkeypatch: pytest.MonkeyPatch) -> None:
    """If cancel POST returns HTTP 409 not-yet-queued then the sweep skips instead of failing."""
    def _raise_409(_args: list[str]) -> str:
        raise PreconditionError(
            "gh api --method POST repos/example/actions/runs/1/cancel failed with exit 1: "
            "gh: Cannot cancel a workflow run that has not been queued yet. (HTTP 409)"
        )

    monkeypatch.setattr(mod, "_run_gh", _raise_409)
    assert mod._cancel_run(1) is mod.CancelOutcome.SKIPPED_NOT_YET_QUEUED


def test_cancel_run_propagates_non_409_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """If cancel POST returns 404 then the precondition error still aborts the sweep."""
    def _raise_404(_args: list[str]) -> str:
        raise PreconditionError(
            "gh api --method POST repos/example/actions/runs/1/cancel failed with exit 1: "
            "gh: Not Found (HTTP 404)"
        )

    monkeypatch.setattr(mod, "_run_gh", _raise_404)
    with pytest.raises(PreconditionError):
        mod._cancel_run(1)


def test_cancel_run_propagates_409_without_not_yet_queued_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If cancel POST returns HTTP 409 without the not-yet-queued reason then it stays loud."""
    def _raise_other_409(_args: list[str]) -> str:
        raise PreconditionError(
            "gh api --method POST repos/example/actions/runs/1/cancel failed with exit 1: "
            "gh: Conflict (HTTP 409)"
        )

    monkeypatch.setattr(mod, "_run_gh", _raise_other_409)
    with pytest.raises(PreconditionError):
        mod._cancel_run(1)


def test_execute_sweep_continues_after_bookkeeping_not_yet_queued_409(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """If one bookkeeping cancel is not yet queued then later targets still cancel."""
    run_skipped = _run(40, name="CI Cost Guard", head_sha=OTHER_SHA)
    run_cancelled = _run(41, name="Stable evidence", head_sha=OTHER_SHA)
    plan = mod.SweepPlan(
        trunk_tip=TRUNK_TIP,
        retained_head_shas=frozenset({TRUNK_TIP}),
        retained_ci_run_ids=frozenset(),
        ci_to_cancel=(),
        bookkeeping_to_cancel=(run_skipped, run_cancelled),
        bookkeeping_kept=0,
    )
    posts: list[int] = []

    def _fake_cancel(run_id: int) -> mod.CancelOutcome:
        posts.append(run_id)
        if run_id == 40:
            return mod.CancelOutcome.SKIPPED_NOT_YET_QUEUED
        return mod.CancelOutcome.CANCELLED

    monkeypatch.setattr(mod, "_cancel_run", _fake_cancel)
    report = mod.execute_sweep(plan, dry_run=False)
    assert posts == [40, 41]
    assert report.bookkeeping_cancelled == 1
    assert report.bookkeeping_cancel_skipped_not_yet_queued == 1
    captured = capsys.readouterr().out
    assert "bookkeeping-cancel-skipped workflow=CI Cost Guard run_id=40" in captured
    assert f"head_sha={OTHER_SHA}" in captured
    assert f"superseded_by={TRUNK_TIP}" in captured
    assert "reason=not-yet-queued" in captured
    assert "::notice::bookkeeping-cancel-skipped" in captured


def test_execute_sweep_propagates_bookkeeping_cancel_precondition_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a bookkeeping cancel fails for a real precondition then the sweep aborts."""
    run = _run(42, name="Error sink", head_sha=OTHER_SHA)
    plan = mod.SweepPlan(
        trunk_tip=TRUNK_TIP,
        retained_head_shas=frozenset({TRUNK_TIP}),
        retained_ci_run_ids=frozenset(),
        ci_to_cancel=(),
        bookkeeping_to_cancel=(run,),
        bookkeeping_kept=0,
    )

    def _raise_precondition(_run_id: int) -> mod.CancelOutcome:
        raise PreconditionError("gh api cancel failed with exit 1: gh: Not Found (HTTP 404)")

    monkeypatch.setattr(mod, "_cancel_run", _raise_precondition)
    with pytest.raises(PreconditionError):
        mod.execute_sweep(plan, dry_run=False)


# ----- R4: CI for a pull request that is no longer open -----
#
# Measured Wed 16 Sep 2026 09:30Z: 13 of 28 queued or running runs belonged to PRs already
# MERGED or CLOSED at the SHA under test (merges land before CI finishes), and they held the
# 13-slot pytest pool while 67 jobs queued behind them.
#
#   - if a queued PR run whose branch has no open PR is not cancelled then broken
#   - if a PR run whose branch has an open PR is cancelled then broken
#   - if an empty open-PR list cancels every PR run instead of refusing then broken
#   - if a push run is selected by the closed-PR sweep then broken


def _pr_run(
    run_id: int, branch: str, *, name: str = "CI", head_repo_owner: str = "maintainer"
) -> mod.QueuedRun:
    return mod.QueuedRun(
        run_id=run_id,
        name=name,
        event="pull_request",
        head_branch=branch,
        head_repo_owner=head_repo_owner,
        head_sha=OTHER_SHA,
        created_at=BASE_TIME,
    )


def _recording_gh_api_json(asked: list[str], result: list[object]) -> Callable[[str], list[object]]:
    """A `_gh_api_json` fake that records every path it was asked and returns `result`."""

    def fake_api(path: str) -> list[object]:
        asked.append(path)
        return result

    return fake_api


def _open_prs(*branches: str, owner: str = "maintainer") -> mod.OpenPullRequests:
    return mod.OpenPullRequests(frozenset((owner, branch) for branch in branches))


def test_a_run_for_a_branch_with_no_open_pr_is_cancelled() -> None:
    runs = [
        _pr_run(50, "af--merged"),
        _pr_run(51, "af--open"),
        _pr_run(52, "af--merged", name="E2E"),
    ]
    assert mod.closed_pr_runs_to_cancel(runs, _open_prs("af--open", "af--other")) == (
        runs[0],
        runs[2],
    )


def test_a_run_for_a_branch_with_an_open_pr_is_kept() -> None:
    runs = [_pr_run(53, "af--open")]
    assert mod.closed_pr_runs_to_cancel(runs, _open_prs("af--open")) == ()


def test_a_measured_empty_open_pr_list_is_a_value_not_a_refusal() -> None:
    """Sol's P1 on #3289, and this test asserted the opposite. A repository whose pull
    requests are ALL closed is exactly the state this sweep exists for, and refusing every
    empty set stopped it working there. `OpenPullRequests` is constructed only by a read that
    COMPLETED (`_open_pr_heads` raises on a failed or malformed page), so an empty one means
    zero measured, which a bare frozenset could not say."""
    run = _pr_run(54, "af--open")
    assert mod.closed_pr_runs_to_cancel([run], mod.OpenPullRequests(frozenset())) == (run,)


def test_an_open_pr_on_another_fork_does_not_retain_this_forks_closed_run() -> None:
    """The owner is half the key. Collapsed to the ref alone, one open `patch-1` retains
    every other fork's closed `patch-1` runs and the queue stall this sweep targets
    persists."""
    run = _pr_run(60, "patch-1", head_repo_owner="someone-else")
    assert mod.closed_pr_runs_to_cancel([run], _open_prs("patch-1")) == (run,)


def test_an_open_pr_on_the_same_fork_still_retains_its_run() -> None:
    """The control against the overshoot: keying on the pair must not stop the owner's own
    open pull request protecting its own live run."""
    run = _pr_run(61, "patch-1", head_repo_owner="someone-else")
    assert (
        mod.closed_pr_runs_to_cancel([run], _open_prs("patch-1", owner="someone-else")) == ()
    )


def test_push_runs_are_never_selected_by_the_closed_pr_sweep() -> None:
    push_run = _run(55, name="CI", event="push")
    assert mod.closed_pr_runs_to_cancel([push_run], _open_prs("af--open")) == ()


def test_closed_pr_sweep_dry_run_posts_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    posts: list[int] = []
    monkeypatch.setattr(mod, "_cancel_run", posts.append)
    counts = mod.execute_closed_pr_sweep(
        (_pr_run(56, "af--merged"),), dry_run=True, still_closed=lambda _: True
    )
    # Sol's P3 on #3289: a dry run PLANS one and CANCELS none. Counted as cancelled, the
    # summary's `closed_pr_cancelled=N` named something other than what it counted.
    assert (counts.planned, counts.cancelled) == (1, 0)
    assert posts == []
    out = capsys.readouterr().out
    assert "closed-pr-cancel workflow=CI run_id=56 head_branch=af--merged" in out


def test_a_branch_reopened_since_the_snapshot_is_not_cancelled(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The snapshot goes stale while the sweep runs. A pull request reopened in that window
    owns a run the list still calls closed, and cancelling it breaks the sweep's one
    contract."""
    posts: list[int] = []
    monkeypatch.setattr(mod, "_cancel_run", posts.append)

    counts = mod.execute_closed_pr_sweep(
        (_pr_run(57, "af--reopened"),), dry_run=False, still_closed=lambda _: False
    )

    assert (counts.planned, counts.cancelled) == (0, 0)
    assert posts == []
    out = capsys.readouterr().out
    assert "closed-pr-skip" in out
    assert "reason=reopened-since-snapshot" in out


def test_a_branch_still_closed_at_the_recheck_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control: the recheck must not stop the sweep doing its job."""
    posts: list[int] = []
    def cancel(run_id: int) -> mod.CancelOutcome:
        posts.append(run_id)
        return mod.CancelOutcome.CANCELLED

    monkeypatch.setattr(mod, "_cancel_run", cancel)

    counts = mod.execute_closed_pr_sweep(
        (_pr_run(58, "af--merged"),), dry_run=False, still_closed=lambda _: True
    )

    assert (counts.planned, counts.cancelled) == (1, 1)
    assert posts == [58]


def test_the_recheck_asks_for_that_branch_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """A recheck that listed every open pull request would be as stale as the snapshot."""
    asked: list[str] = []

    def fake_api(path: str):
        asked.append(path)
        return []

    monkeypatch.setattr(mod, "_gh_api_json", fake_api)
    assert mod._open_pr_count("maintainer", "af--thing") == 0
    assert asked == [
        f"repos/{mod.REPO}/pulls?state=open&head=maintainer:af--thing&per_page=1"
    ]


def test_the_recheck_reports_an_open_pull_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "_gh_api_json", lambda path: [{"number": 1}])
    assert mod._open_pr_count("maintainer", "af--open") == 1


def test_the_recheck_refuses_a_payload_that_is_not_a_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """A message object counts len() too, and would read as "some open pull requests"."""
    monkeypatch.setattr(mod, "_gh_api_json", lambda path: {"message": "Not Found"})
    with pytest.raises(PreconditionError):
        mod._open_pr_count("maintainer", "af--thing")


def test_the_trunk_tip_workflow_can_list_pull_requests() -> None:
    """Unspecified workflow permissions are DISABLED, so without this the sweep reads
    "Resource not accessible by integration" and cancels nothing, silently."""
    workflow = (
        Path(__file__).resolve().parents[2] / ".github" / "workflows" / "trunk-tip-only.yml"
    )
    permissions = yaml.safe_load(workflow.read_text(encoding="utf-8"))["permissions"]
    assert permissions.get("pull-requests") == "read", permissions


# ----- a fork pull request is rechecked under its own owner -----


def test_the_recheck_asks_under_the_forks_owner_not_this_repositorys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sol's P1 on #3289. A fork's head branch is owned by the fork, so asking under this
    repository's owner returns nothing, reads as "no open pull request", and cancels a live
    run. That is the single contract the recheck exists to hold."""
    asked: list[str] = []
    monkeypatch.setattr(mod, "_gh_api_json", _recording_gh_api_json(asked, [{"number": 9}]))
    run = _pr_run(60, "patch-1", head_repo_owner="a-contributor")
    counts = mod.execute_closed_pr_sweep((run,), dry_run=False)
    assert asked == [
        f"repos/{mod.REPO}/pulls?state=open&head=a-contributor:patch-1&per_page=1"
    ]
    assert (counts.planned, counts.cancelled) == (0, 0)


def test_a_run_from_this_repository_is_still_asked_under_this_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control: the ordinary case is the overwhelming majority and must not move."""
    asked: list[str] = []
    monkeypatch.setattr(mod, "_gh_api_json", _recording_gh_api_json(asked, []))
    monkeypatch.setattr(mod, "_cancel_run", lambda _run_id: mod.CancelOutcome.CANCELLED)
    run = _pr_run(61, "af--merged")
    counts = mod.execute_closed_pr_sweep((run,), dry_run=False)
    assert (counts.planned, counts.cancelled) == (1, 1)
    assert asked == [
        f"repos/{mod.REPO}/pulls?state=open&head=maintainer:af--merged&per_page=1"
    ]


def test_a_run_payload_with_no_head_repository_is_refused() -> None:
    """A run whose head repository is absent cannot be rechecked under any owner. Defaulting
    to this repository's owner would put the fork bug back, silently, for that run alone."""
    with pytest.raises(PreconditionError):
        mod._parse_queued_run(
            {
                "id": 1,
                "name": "CI",
                "event": "pull_request",
                "head_branch": "patch-1",
                "head_sha": OTHER_SHA,
                "created_at": "2026-09-16T09:00:00Z",
            }
        )


def test_a_run_payload_carrying_its_head_repository_is_parsed() -> None:
    """The control: the field the refusal above keys on is one GitHub really sends."""
    run = mod._parse_queued_run(
        {
            "id": 1,
            "name": "CI",
            "event": "pull_request",
            "head_branch": "patch-1",
            "head_repository": {"owner": {"login": "a-contributor"}},
            "head_sha": OTHER_SHA,
            "created_at": "2026-09-16T09:00:00Z",
        }
    )
    assert run.head_repo_owner == "a-contributor"


def test_a_branch_name_carrying_a_query_delimiter_is_encoded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sol's second P1 on #3289. `&` legally appears in a git branch name; interpolated raw
    it starts a new query parameter, so the filter asks about a different branch, finds
    nothing, and the sweep cancels a live run. `+` is as bad and quieter: it decodes to a
    space."""
    asked: list[str] = []
    monkeypatch.setattr(mod, "_gh_api_json", _recording_gh_api_json(asked, []))
    mod._open_pr_count("maintainer", "af--a&state=closed+b")
    assert asked == [
        f"repos/{mod.REPO}/pulls"
        "?state=open&head=maintainer:af--a%26state%3Dclosed%2Bb&per_page=1"
    ]


def test_an_ordinary_branch_name_is_left_readable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The control: `:` and `-` carry the head filter's own syntax and must not be escaped,
    or every ordinary branch stops matching and the sweep cancels everything."""
    asked: list[str] = []
    monkeypatch.setattr(mod, "_gh_api_json", _recording_gh_api_json(asked, []))
    mod._open_pr_count("maintainer", "af--ci-watch")
    assert asked == [
        f"repos/{mod.REPO}/pulls?state=open&head=maintainer:af--ci-watch&per_page=1"
    ]


def test_a_run_seen_in_both_status_snapshots_is_swept_once() -> None:
    """Sol's P2 on #3289. `queued` and `in_progress` are two requests, so a run that STARTS
    between them appears in both. Concatenated it is logged twice and cancellation attempted
    twice, and the reported count measures the race rather than the work."""
    queued = _pr_run(70, "af--merged")
    in_progress = _pr_run(70, "af--merged", name="CI")
    assert mod.unique_runs([queued, in_progress]) == [queued]


def test_two_genuinely_different_runs_are_both_kept() -> None:
    """The control against the overshoot: deduplicating on anything coarser than the run id
    would silently drop a second real run for the same branch, and the sweep would leave the
    queue stall it exists to clear."""
    first = _pr_run(71, "af--merged")
    second = _pr_run(72, "af--merged", name="E2E")
    assert mod.unique_runs([first, second]) == [first, second]


def test_a_malformed_open_pr_page_raises_instead_of_yielding_an_empty_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The negative control that makes an empty snapshot safe to trust. Dropping the refusal
    in the pure selector is only correct while NO failed read can produce an
    `OpenPullRequests` at all: this is what carries that claim, and without it an empty
    snapshot would mean either zero open pull requests or a read that fell over."""
    monkeypatch.setattr(mod, "_gh_api_json", lambda _path: {"message": "Bad credentials"})
    with pytest.raises(PreconditionError):
        mod._open_pr_heads()


def test_an_open_pr_without_a_head_owner_raises_rather_than_defaulting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defaulted to this repository's owner, a fork pull request reads as a DIFFERENT pull
    request, so its live run looks closed and is cancelled."""
    monkeypatch.setattr(mod, "_gh_api_json", lambda _path: [{"head": {"ref": "patch-1"}}])
    with pytest.raises(PreconditionError):
        mod._open_pr_heads()


def test_a_complete_read_of_zero_open_pulls_is_an_empty_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control on the other side: a read that COMPLETES and finds nothing must produce a
    snapshot, not an exception, or the refusal has simply moved one function along."""
    monkeypatch.setattr(mod, "_gh_api_json", lambda _path: [])
    assert mod._open_pr_heads() == mod.OpenPullRequests(frozenset())
