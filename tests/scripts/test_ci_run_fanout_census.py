"""Unit tests for scripts.ci_run_fanout_census (no network)."""

from __future__ import annotations

import io
from typing import Any

from scripts.ci_run_fanout_census import (
    CensusError,
    census,
    group_counts,
    main,
    parse_time,
    select_runs,
)

RECORDED_PAGE: dict[str, Any] = {
    "total_count": 7,
    "workflow_runs": [
        {
            "id": 1,
            "name": "CI",
            "event": "pull_request",
            "status": "queued",
            "created_at": "2026-11-06T01:00:00Z",
        },
        {
            "id": 2,
            "name": "CI",
            "event": "pull_request",
            "status": "in_progress",
            "created_at": "2026-11-06T01:05:00Z",
        },
        {
            "id": 3,
            "name": "CI Cost Guard",
            "event": "workflow_run",
            "status": "queued",
            "created_at": "2026-11-06T01:10:00Z",
        },
        {
            "id": 4,
            "name": "Stable evidence",
            "event": "schedule",
            "status": "queued",
            "created_at": "2026-11-06T01:15:00Z",
        },
        {
            "id": 5,
            "name": "PR labels",
            "event": "pull_request",
            "status": "queued",
            "created_at": "2026-11-06T01:20:00Z",
        },
        {
            "id": 6,
            "name": "CI Cost Guard",
            "event": "workflow_run",
            "status": "queued",
            "created_at": "2026-09-22T00:00:00Z",
        },
        {
            "id": 7,
            "name": "CI",
            "event": "pull_request",
            "status": "completed",
            "created_at": "2026-11-06T01:25:00Z",
        },
    ],
}

NOW = "2026-11-06T12:00:00Z"
WINDOW_HOURS = 24


def _window_bounds() -> tuple[Any, Any]:
    end = parse_time(NOW)
    from datetime import timedelta

    start = end - timedelta(hours=WINDOW_HOURS)
    return start, end


def test_per_workflow_event_counts_and_kinds() -> None:
    start, end = _window_bounds()
    kept, excluded = select_runs(RECORDED_PAGE["workflow_runs"], start=start, end=end)
    assert excluded >= 1
    assert 6 not in {r["id"] for r in kept}
    assert 7 not in {r["id"] for r in kept}

    counts = group_counts(kept)
    assert counts[("CI", "pull_request")] == 2
    assert counts[("CI Cost Guard", "workflow_run")] == 1
    assert counts[("Stable evidence", "schedule")] == 1
    assert counts[("PR labels", "pull_request")] == 1

    paths: list[str] = []

    def get_json(path: str) -> dict[str, Any]:
        paths.append(path)
        return RECORDED_PAGE

    result = census(
        "private_owner/music-dj-tools",
        start=start,
        end=end,
        get_json=get_json,
    )
    assert result["total"] == 5
    assert result["bookkeeping"] == 3
    assert result["verdict"] == 2
    assert result["excluded_before_window"] >= 1
    assert any("status=queued" in p for p in paths)
    assert any("status=in_progress" in p for p in paths)


def test_pre_window_run_excluded() -> None:
    start, end = _window_bounds()
    kept, excluded = select_runs(RECORDED_PAGE["workflow_runs"], start=start, end=end)
    ids = {r["id"] for r in kept}
    assert 6 not in ids
    assert excluded >= 1


def test_api_failure_prints_no_counts() -> None:
    def boom(_path: str) -> dict[str, Any]:
        raise CensusError("gh api repos/o/r/actions/runs exited 1: boom")

    stdout = io.StringIO()
    stderr = io.StringIO()
    code = main(
        ["--now", NOW, "--window-hours", str(WINDOW_HOURS)],
        get_json=boom,
        stdout=stdout,
        stderr=stderr,
    )
    assert code != 0
    assert "error:" in stderr.getvalue()
    assert "boom" in stderr.getvalue()
    assert "total=" not in stdout.getvalue()
    assert "\t" not in stdout.getvalue()


def test_empty_page_success_prints_zero_total() -> None:
    empty: dict[str, Any] = {"total_count": 0, "workflow_runs": []}

    def get_json(_path: str) -> dict[str, Any]:
        return empty

    stdout = io.StringIO()
    stderr = io.StringIO()
    code = main(
        ["--now", NOW, "--window-hours", str(WINDOW_HOURS)],
        get_json=get_json,
        stdout=stdout,
        stderr=stderr,
    )
    assert code == 0
    assert stderr.getvalue() == ""
    assert "total=0" in stdout.getvalue()
