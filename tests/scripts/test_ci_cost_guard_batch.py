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
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta

import pytest

from scripts.ci_cost_guard import render_batch_summary, select_batch_runs
from scripts.ci_run_batch import (
    batch_since,
    created_slices,
    fetch_completed_runs,
    fetch_inflight_runs,
    last_successful_pass_start,
    parse_time,
    runs_held_back,
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


def _github(created: dict[str, list[datetime]]) -> tuple[Callable[[str], dict], list[str]]:
    """A fake GET over named workflows. The workflows index maps each name to an id; a runs
    listing returns that workflow's runs created inside `created=A..B` (inclusive), newest
    first, ONE page of 100 at most, the way GitHub serves page 1. Every URL is recorded."""
    names = sorted(created)
    requested: list[str] = []

    def get_json(url: str) -> dict:
        requested.append(url)
        if "/actions/workflows?" in url:
            return {"workflows": [{"id": 10 + k, "name": n} for k, n in enumerate(names)]}
        query = re.search(
            r"/actions/workflows/(\d+)/runs\?.*created=([0-9TZ:-]+)\.\.([0-9TZ:-]+)", url
        )
        assert query, url
        name = names[int(query.group(1)) - 10]
        start, stop = (parse_time(query.group(k)) for k in (2, 3))
        matching = sorted(
            (k for k, when in enumerate(created[name]) if start <= when <= stop), reverse=True
        )
        return {"workflow_runs": [{"id": f"{name}-{k}", "name": name} for k in matching[:100]]}

    return get_json, requested


START = datetime(2026, 9, 22, 0, 0, tzinfo=UTC)


def _list(created: dict[str, list[datetime]], names: set[str]) -> tuple[list[dict], list[str]]:
    get, requested = _github(created)
    runs = fetch_completed_runs(
        "o/r", "2026-09-22T00:00:00Z", "t", "test",
        workflow_names=names, now=START + timedelta(hours=2), get_json=get,
    )
    return runs, requested


def test_only_the_named_workflows_are_listed_and_every_run_once() -> None:
    created = {
        "CI": [START + timedelta(minutes=m) for m in range(0, 120, 7)],
        "Other": [START + timedelta(minutes=5)],
    }
    created["CI"].append(START + timedelta(hours=1))  # on the slice boundary, both sides
    runs, requested = _list(created, {"CI"})
    assert sorted(run["id"] for run in runs) == sorted(f"CI-{k}" for k in range(len(created["CI"])))
    assert not any("/workflows/11/" in url for url in requested), "Other was never listed"


def test_every_listing_is_one_page_so_no_rerun_can_shift_a_run_across_pages() -> None:
    """if a slice is read across pages then a run re-run mid-listing leaves the completed set
    and shifts a later run into the page already read, which is then skipped for good (Codex
    P1 on #3844): a full page is bisected instead of paged, and the dense hour still lists
    every run"""
    created = {"CI": [START + timedelta(seconds=k * 1.44) for k in range(2500)]}
    runs, requested = _list(created, {"CI"})
    assert {run["id"] for run in runs} == {f"CI-{k}" for k in range(2500)}
    assert not any(re.search(r"[?&]page=", url) for url in requested if "/runs?" in url)


def test_a_full_page_at_one_second_fails_closed() -> None:
    created = {"CI": [START + timedelta(seconds=30)] * 100}
    with pytest.raises(RuntimeError, match="one second"):
        _list(created, {"CI"})


def test_a_watched_name_with_no_workflow_fails_closed() -> None:
    """if a watched name matches no workflow then the pass reads nothing for it and looks
    clean"""
    with pytest.raises(RuntimeError, match="Full CI"):
        _list({"CI": []}, {"CI", "Full CI"})


# ----- the mark: the last SUCCESSFUL pass, however far back (Codex P1s on #3844) -----


def _started(index: int) -> str:
    return f"2026-09-{29 - (index // 12):02d}T{12 - index % 12:02d}:00:00Z"


def _passes(conclusions: Sequence[str | None], this_run: int = 1) -> Callable[[str], dict]:
    """Newest-first passes of the follower, 100 per page; pass i started i hours before
    12:00 on Tue 29 Sep, and id 1 is this pass. A None conclusion is still in flight."""
    runs = [
        {
            "id": this_run + index,
            "status": "in_progress" if conclusion is None else "completed",
            "conclusion": conclusion,
            "run_started_at": _started(index),
        }
        for index, conclusion in enumerate(conclusions)
    ]

    def get(url: str) -> dict:
        # if the history query carries a status (or any capped) filter then GitHub stops it
        # at 1,000 results and the true last success can sit past the end
        assert "status=" not in url and "created=" not in url and "per_page=100" in url, url
        query = re.search(r"[?&]page=(\d+)", url)
        assert query, url
        page = int(query.group(1))
        return {"workflow_runs": runs[(page - 1) * 100 : page * 100]}

    return get


def test_mark_skips_failed_passes_in_flight_passes_and_this_run() -> None:
    get = _passes(["success", None, "failure", "success"])
    assert last_successful_pass_start("o/r", "f.yml", 1, get_json=get) == _started(3)


def test_mark_searches_the_whole_history_with_no_page_cap() -> None:
    get = _passes(["failure"] * 1250 + ["success"])
    assert last_successful_pass_start("o/r", "f.yml", 999, get_json=get) == _started(1250)


def test_mark_with_no_success_anywhere_covers_since_the_oldest_pass() -> None:
    get = _passes(["failure"] * 3)
    assert last_successful_pass_start("o/r", "f.yml", 999, get_json=get) == _started(2)


def test_mark_with_no_passes_at_all_is_empty() -> None:
    assert last_successful_pass_start("o/r", "f.yml", 1, get_json=_passes([])) == ""


# ----- the hold: a watched run older than the lookback keeps the mark where it is -----

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def _inflight(run_id: int, name: str, created: str) -> dict:
    return {"id": run_id, "name": name, "status": "queued", "created_at": created}


def test_a_watched_run_created_before_the_lookback_holds_the_mark() -> None:
    """if a run queued longer than the lookback lets the pass succeed then its completion is
    never listed again and is lost"""
    inflight = [
        _inflight(1, "CI", "2026-09-29T05:59:00Z"),
        _inflight(2, "CI", "2026-09-29T06:01:00Z"),
        _inflight(3, "Unwatched", "2026-09-28T00:00:00Z"),
    ]
    held = runs_held_back(inflight, {"CI"}, NOW, timedelta(hours=6))
    assert [run["id"] for run in held] == [1]


def test_no_in_flight_straggler_lets_the_pass_advance() -> None:
    inflight = [_inflight(2, "CI", "2026-09-29T06:01:00Z")]
    assert runs_held_back(inflight, {"CI"}, NOW, timedelta(hours=6)) == []


def test_the_census_reads_one_page_of_old_runs_per_status() -> None:
    requested: list[str] = []

    def get(url: str) -> dict:
        requested.append(url)
        return {"workflow_runs": [_inflight(1, "CI", "2026-09-29T05:00:00Z")]}

    runs = fetch_inflight_runs(
        "o/r", "t", "test", created_before="2026-09-29T06:00:00Z", get_json=get
    )
    assert [run["id"] for run in runs] == [1]
    assert all("created=<2026-09-29T06:00:00Z" in url for url in requested)
    assert not any(re.search(r"[?&]page=", url) for url in requested)


def test_a_census_status_that_fills_a_page_fails_closed() -> None:
    def get(url: str) -> dict:
        return {"workflow_runs": [_inflight(k, "CI", "2026-09-29T05:00:00Z") for k in range(100)]}

    with pytest.raises(RuntimeError, match="second page"):
        fetch_inflight_runs("o/r", "t", "test", created_before="2026-09-29T06:00:00Z", get_json=get)
