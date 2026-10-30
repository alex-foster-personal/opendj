"""ci.yml main pushes are tip-only: one running run, at most one pending, never one per merge.

TEST-BUDGET follow-up (Fri 30 Oct 2026). Under direct landing every merge queued a full ci.yml
run (13 main runs queued at 17:40Z, none started in 50 minutes, the runner pool saturated). The
per-commit group the Mon 31 Aug 2026 incident introduced is replaced by one shared push group;
the checkpoint-and-revert lane (fleet-af PR 1020) covers a superseded run from the tip's run.

Single-line intent:
  - [if] push runs keep a per-SHA group [then] every merge queues a full run and the pool saturates, [else stop].
  - [if] push runs cancel the running run [then] merges faster than a run leave main with no completed run, [else stop].
  - [if] pull requests stop cancelling in progress [then] a new head waits behind its stale run, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import yaml

CI = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"


def _concurrency() -> dict:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))["concurrency"]


def test_push_runs_share_one_group_without_a_sha() -> None:
    """[if] push runs keep a per-SHA group [then] every merge queues a full run, [else stop]."""
    group = " ".join(str(_concurrency()["group"]).split())
    assert "github.sha" not in group
    assert "github.event_name == 'push' && 'main-push'" in group
    assert group.endswith("|| 'shared' }}")


def test_push_runs_never_cancel_the_running_run_and_prs_still_do() -> None:
    """[if] a push cancels in progress, or a PR stops cancelling, [then] fail, [else stop]."""
    assert _concurrency()["cancel-in-progress"] == "${{ github.event_name != 'push' }}"
