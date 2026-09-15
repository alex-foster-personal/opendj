"""Tests for :mod:`scripts.ci_trunk_tip_only`.

Regression lines:
  - if a superseded Stable evidence run stays queued after sweep then broken
  - if dry_run performs a cancel POST then broken
  - if retained CI push runs are not exactly oldest and newest then broken
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from scripts import ci_trunk_tip_only as mod

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
        _run(11, name="CI Cost Guard", head_sha=TRUNK_TIP),
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

    def _fake_cancel(run_id: int) -> None:
        posts.append(run_id)

    monkeypatch.setattr(mod, "_cancel_run", _fake_cancel)
    mod.execute_sweep(plan, dry_run=False)
    assert posts == [31]


def test_cancel_log_line_includes_superseded_by() -> None:
    """Cancellation logs name the superseding trunk tip SHA."""
    run = _run(32, name="CI Cost Guard", head_sha=OTHER_SHA)
    line = mod._cancel_log_line(run, TRUNK_TIP)
    assert "superseded_by=" + TRUNK_TIP in line
    assert "workflow=CI Cost Guard" in line
    assert f"run_id={run.run_id}" in line
