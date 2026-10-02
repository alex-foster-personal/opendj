"""e2e.yml main pushes are tip-only; pull requests still supersede their own runs.

TEST-CUT (specs/ci-test-cut.md, Fri 2 Oct 2026). Every main push is a tree a Trunk queue
draft already ran the e2e gate on, so a per-SHA push group tested each merge twice on
the four `e2e` runners drafts wait for (14 push gates in 6.7 h waited a median 79 min).

Single-line intent:
  - if push runs keep a per-SHA group then every merge queues a second e2e gate
  - if push runs cancel in progress then a burst of merges can leave main with no verdict
  - if pull requests stop cancelling in progress then a new push waits behind its stale run
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.requirement("INFRA-03")

E2E = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "e2e.yml"


def _concurrency() -> dict:
    return yaml.safe_load(E2E.read_text(encoding="utf-8"))["concurrency"]


def test_push_runs_share_one_group_without_a_sha() -> None:
    """if push runs keep a per-SHA group then every merge queues a second e2e gate"""
    group = _concurrency()["group"]
    assert "github.sha" not in group
    assert "github.event_name == 'push' && 'push-tip'" in group
    assert group.endswith("|| 'shared' }}")


def test_push_runs_never_cancel_a_running_gate_and_prs_still_do() -> None:
    """if a push cancels in progress then a merge burst leaves main with no verdict; if a PR
    stops cancelling then a new head waits behind its stale run"""
    assert _concurrency()["cancel-in-progress"] == "${{ github.event_name != 'push' }}"
