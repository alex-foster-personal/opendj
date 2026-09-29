"""The cost guard's batch pass: which completed runs one scheduled pass prices.

One scheduled job prices every watched completion since a high-water mark
instead of one workflow_run job per completion (RUN-COUNT round 1, #2196).
The mark is the previous pass's own start time, read back from GitHub, with
a floor so two passes always overlap; the alert issue's title carries the run
id, which is what makes re-pricing an overlap harmless.

[if] a completion since the mark is skipped or E2E is priced off-rule [then] fail, [else stop].
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from scripts.ci_cost_guard import render_batch_summary, select_batch_runs
from scripts.ci_run_batch import (
    RESULT_CAP,
    batch_since,
    created_slices,
    fetch_completed_runs,
    last_successful_pass_start,
)

pytestmark = pytest.mark.requirement("OPS-36")

WATCHED = {"CI", "E2E", "macOS Packaging"}
E2E_EVENTS = {"schedule", "workflow_dispatch"}
MARK = "2026-09-22T19:00:00Z"


def _run(
    run_id: int,
    name: str,
    *,
    event: str = "pull_request",
    updated: str = "2026-09-22T19:30:00Z",
    status: str = "completed",
    conclusion: str | None = "success",
) -> dict:
    return {
        "id": run_id,
        "name": name,
        "event": event,
        "status": status,
        "conclusion": conclusion,
        "run_attempt": 1,
        "updated_at": updated,
        "html_url": f"https://x/{run_id}",
    }


def test_a_watched_completion_after_the_mark_is_selected_and_an_unwatched_one_is_not() -> None:
    runs = [_run(1, "CI"), _run(2, "Build docs"), _run(3, "macOS Packaging", event="create")]
    assert [r["id"] for r in select_batch_runs(runs, WATCHED, E2E_EVENTS, MARK)] == [1, 3]


def test_a_completion_before_the_mark_or_still_running_is_not_selected() -> None:
    runs = [
        _run(1, "CI", updated="2026-09-22T18:59:59Z"),
        _run(2, "CI", status="in_progress", conclusion=None),
    ]
    assert select_batch_runs(runs, WATCHED, E2E_EVENTS, MARK) == []


def test_a_cancelled_run_is_still_priced_because_a_cancelled_timeout_bills_the_ceiling() -> None:
    runs = [_run(1, "CI", conclusion="cancelled")]
    assert [r["id"] for r in select_batch_runs(runs, WATCHED, E2E_EVENTS, MARK)] == [1]


@pytest.mark.parametrize(
    "event,priced",
    [("pull_request", False), ("push", False), ("schedule", True), ("workflow_dispatch", True)],
)
def test_e2e_is_priced_only_on_the_events_that_can_reach_its_ceiling(
    event: str, priced: bool
) -> None:
    selected = select_batch_runs([_run(1, "E2E", event=event)], WATCHED, E2E_EVENTS, MARK)
    assert bool(selected) is priced


def test_the_mark_is_the_previous_pass_start_but_never_later_than_the_overlap_floor() -> None:
    now = datetime(2026, 9, 22, 20, 0, tzinfo=UTC)
    floor = timedelta(minutes=30)
    assert batch_since("2026-09-22T19:50:00Z", now, floor) == "2026-09-22T19:30:00Z"
    assert batch_since("2026-09-22T19:10:00Z", now, floor) == "2026-09-22T19:10:00Z"
    assert batch_since(None, now, floor) == "2026-09-22T19:30:00Z"


def test_the_summary_lists_every_priced_run_and_alerts_only_above_threshold_or_unpriced() -> None:
    priced = [
        {
            "run": _run(1, "CI"),
            "total_cost": 0.05,
            "over_threshold": False,
            "unknown_jobs": [],
            "report_file": "r1.md",
        },
        {
            "run": _run(2, "CI"),
            "total_cost": 0.40,
            "over_threshold": True,
            "unknown_jobs": [],
            "report_file": "r2.md",
        },
        {
            "run": _run(3, "E2E", event="schedule"),
            "total_cost": 0.0,
            "over_threshold": False,
            "unknown_jobs": [{"name": "x"}],
            "report_file": "r3.md",
        },
    ]
    summary, alerts = render_batch_summary(priced, threshold=0.30, since=MARK)
    assert "| 1 |" in summary and "| 2 |" in summary and "| 3 |" in summary
    assert [a["run_id"] for a in alerts] == [2, 3]
    assert alerts[0]["title"] == "CI cost alert: run 2 estimated at $0.400"
    assert alerts[1]["title"] == "CI cost telemetry alert: unpriced runner in run 3"
    assert alerts[0]["report_file"] == "r2.md"


# ----- the listing: sliced below GitHub's 1,000-result cap on filtered searches ------


def test_created_slices_cover_the_window_end_to_end() -> None:
    now = datetime(2026, 9, 22, 22, 30, tzinfo=UTC)
    slices = created_slices("2026-09-22T20:00:00Z", now, timedelta(hours=1))
    assert slices == [
        ("2026-09-22T20:00:00Z", "2026-09-22T21:00:00Z"),
        ("2026-09-22T21:00:00Z", "2026-09-22T22:00:00Z"),
        ("2026-09-22T22:00:00Z", "2026-09-22T22:30:00Z"),
    ]


def _pages(per_slice: int) -> Callable[[str], dict]:
    """A fake GET: `per_slice` runs per one-hour creation slice, 100 per page, ids
    unique per slice except that each later slice's first id repeats the previous
    slice's last (the boundary second is inclusive on both ends)."""

    def get_json(url: str) -> dict:
        query = re.search(r"&page=(\d+)&created=(\d{4}-\d\d-\d\dT(\d\d))", url)
        assert query, url
        page, slice_index = int(query.group(1)), int(query.group(3))
        first = slice_index * per_slice
        ids = list(range(first, first + per_slice))
        if slice_index:
            ids[0] = first - 1
        chunk = ids[(page - 1) * 100 : page * 100]
        return {"workflow_runs": [{"id": run_id} for run_id in chunk]}

    return get_json


def test_listing_dedupes_the_inclusive_slice_boundary() -> None:
    now = datetime(2026, 9, 22, 2, 0, tzinfo=UTC)
    runs = fetch_completed_runs(
        "o/r", "2026-09-22T00:00:00Z", "t", "test", now=now, get_json=_pages(150)
    )
    assert len(runs) == len({run["id"] for run in runs}) == 299


def test_listing_fails_closed_when_a_slice_reaches_the_result_cap() -> None:
    now = datetime(2026, 9, 22, 1, 0, tzinfo=UTC)
    with pytest.raises(RuntimeError, match=str(RESULT_CAP)):
        fetch_completed_runs(
            "o/r", "2026-09-22T00:00:00Z", "t", "test", now=now, get_json=_pages(RESULT_CAP)
        )


# ----- the mark: the last SUCCESSFUL pass, however far back (Codex P1s on #3844) -----


def _started(index: int) -> str:
    return f"2026-09-{29 - (index // 12):02d}T{12 - index % 12:02d}:00:00Z"


def _passes(conclusions: list[str], this_run: int = 1) -> Callable[[str], dict]:
    """Newest-first completed passes of the follower, 100 per page; pass i started i hours
    before 12:00 on Tue 29 Sep, and id 1 is this pass."""
    runs = [
        {"id": this_run + index, "conclusion": conclusion, "run_started_at": _started(index)}
        for index, conclusion in enumerate(conclusions)
    ]

    def get(url: str) -> dict:
        page = int(re.search(r"[?&]page=(\d+)", url).group(1))
        assert "status=completed" in url and "per_page=100" in url, url
        return {"workflow_runs": runs[(page - 1) * 100 : page * 100]}

    return get


def test_mark_skips_failed_passes_and_this_run() -> None:
    get = _passes(["success", "failure", "failure", "success"])
    assert last_successful_pass_start("o/r", "f.yml", 1, get_json=get) == "2026-09-29T09:00:00Z"


def test_mark_searches_past_the_first_page() -> None:
    get = _passes(["failure"] * 150 + ["success"])
    start = last_successful_pass_start("o/r", "f.yml", 999, get_json=get)
    assert start == _started(150)


def test_mark_with_no_success_anywhere_covers_since_the_oldest_pass() -> None:
    get = _passes(["failure"] * 3)
    assert last_successful_pass_start("o/r", "f.yml", 999, get_json=get) == "2026-09-29T10:00:00Z"


def test_mark_with_no_passes_at_all_is_empty() -> None:
    assert last_successful_pass_start("o/r", "f.yml", 1, get_json=_passes([])) == ""


def test_mark_stops_at_the_page_cap_and_covers_since_the_oldest_seen() -> None:
    get = _passes(["failure"] * 250)
    start = last_successful_pass_start("o/r", "f.yml", 999, get_json=get, max_pages=2)
    assert start == _started(199)
