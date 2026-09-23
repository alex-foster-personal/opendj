"""Stable evidence's batch pass: which completions one scheduled pass records.

RUN-COUNT round 2 (issue #2196): the per-completion workflow_run job became one
scheduled pass. The selection rule must keep every exclusion the per-completion
job had (cancelled runs, `CI` dispatch runs) and apply completions oldest first,
so the last completion for a sha still wins, and re-applying an overlap changes
nothing.

Regression lines:
  - if a cancelled run or a `CI` workflow_dispatch run is recorded, then broken
  - if a completion since the mark of a recorded workflow is dropped, then broken
  - if two completions for one sha and suite both reach the file, then broken
  - if re-applying an already recorded run bumps written_at_utc, then broken

[if] a cancelled or dispatch run is recorded, or a completion is dropped [then] fail, [else stop].
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from scripts.stable_evidence import evidence_path, load_evidence
from scripts.stable_evidence_batch import append_suite_runs, select_suite_runs

pytestmark = pytest.mark.requirement("OPS-18")

SHA = "a" * 40
OTHER_SHA = "b" * 40
SINCE = "2026-09-22T19:00:00Z"


def _run(
    run_id: int,
    name: str = "CI",
    *,
    updated_at: str = "2026-09-22T19:30:00Z",
    conclusion: str | None = "success",
    event: str = "push",
    status: str = "completed",
    head_sha: str = SHA,
) -> dict[str, Any]:
    return {
        "id": run_id,
        "name": name,
        "event": event,
        "status": status,
        "conclusion": conclusion,
        "updated_at": updated_at,
        "head_sha": head_sha,
    }


def test_every_recorded_workflow_completion_since_the_mark_is_selected() -> None:
    runs = [
        _run(1, "CI"),
        _run(2, "Full CI (on-demand)"),
        _run(3, "E2E"),
        _run(4, "macOS Packaging", conclusion="skipped"),
        _run(5, "CI Cost Guard"),
        _run(6, "CI", updated_at="2026-09-22T18:59:59Z"),
        _run(7, "CI", status="in_progress", conclusion=None),
    ]
    assert [run["id"] for run in select_suite_runs(runs, SINCE)] == [1, 2, 3, 4]


def test_cancelled_runs_are_never_recorded() -> None:
    runs = [_run(1, "CI", conclusion="cancelled"), _run(2, "E2E", conclusion="cancelled")]
    assert select_suite_runs(runs, SINCE) == []


def test_a_ci_dispatch_run_is_never_recorded_but_a_dispatched_full_ci_is() -> None:
    """The six-hour `tier: fast` control dispatch completes under the name `CI` and
    would overwrite a failed push run with a green partial one (Codex P1 on #3651)."""
    runs = [
        _run(1, "CI", event="workflow_dispatch"),
        _run(2, "Full CI (on-demand)", event="workflow_dispatch"),
        _run(3, "CI", event="push"),
    ]
    assert [run["id"] for run in select_suite_runs(runs, SINCE)] == [2, 3]


def test_one_completion_per_sha_and_suite_the_newest(tmp_path: Path) -> None:
    """Two completions for one sha and suite in one window coalesce to the newest
    BEFORE any write, so a pass that dies mid-batch never leaves the older result
    in the file (Codex P1 on #3844)."""
    runs = [
        _run(1, "CI", updated_at="2026-09-22T19:20:00Z", conclusion="success"),
        _run(2, "CI", updated_at="2026-09-22T19:40:00Z", conclusion="failure"),
        _run(3, "E2E", updated_at="2026-09-22T19:30:00Z"),
    ]
    chosen = select_suite_runs(runs, SINCE)
    assert [run["id"] for run in chosen] == [3, 2]
    written = append_suite_runs(tmp_path, chosen, "github-actions")
    assert [run["id"] for run, _ in written] == [3, 2]
    body = load_evidence(evidence_path(tmp_path, SHA))
    assert body["suites"]["fast_lane"] == {"run_id": "2", "conclusion": "failure"}


def test_a_rerun_of_the_same_run_id_replaces_its_earlier_attempt() -> None:
    runs = [
        _run(1, "CI", updated_at="2026-09-22T19:20:00Z", conclusion="failure"),
        _run(1, "CI", updated_at="2026-09-22T19:50:00Z", conclusion="success"),
    ]
    assert [run["conclusion"] for run in select_suite_runs(runs, SINCE)] == ["success"]


def test_append_suite_runs_records_each_run_under_its_sha_and_suite(tmp_path: Path) -> None:
    runs = [
        _run(2, "CI", updated_at="2026-09-22T19:40:00Z", conclusion="success"),
        _run(3, "E2E", updated_at="2026-09-22T19:30:00Z", head_sha=OTHER_SHA),
    ]
    written = append_suite_runs(tmp_path, select_suite_runs(runs, SINCE), "github-actions")
    assert [run["id"] for run, _ in written] == [3, 2]
    body = load_evidence(evidence_path(tmp_path, SHA))
    assert body["suites"]["fast_lane"] == {"run_id": "2", "conclusion": "success"}
    assert body["written_by"] == "github-actions:CI"
    other = load_evidence(evidence_path(tmp_path, OTHER_SHA))
    assert other["suites"]["e2e"] == {"run_id": "3", "conclusion": "success"}
    assert other["written_by"] == "github-actions:E2E"


def test_reapplying_an_overlap_writes_nothing(tmp_path: Path) -> None:
    runs = select_suite_runs([_run(1, "CI"), _run(2, "E2E")], SINCE)
    append_suite_runs(tmp_path, runs, "github-actions")
    stamp = load_evidence(evidence_path(tmp_path, SHA))["written_at_utc"]
    written = append_suite_runs(tmp_path, runs, "github-actions")
    assert written == []
    assert load_evidence(evidence_path(tmp_path, SHA))["written_at_utc"] == stamp
