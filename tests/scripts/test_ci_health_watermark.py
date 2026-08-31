"""Tests for R4 watermark catch-up in :mod:`scripts.ci_health_metrics`.

Split from test_ci_health_metrics.py to keep both files under the repo's 600-line quality
gate; the shared run/job payload fixtures are imported from there.

The incident this file pins, Sun 31 Aug 2026: _fetch_jobs took a fixed newest-10 slice of
the runs checks 1-4 had already fetched, on the documented assumption that 10 "covers the
[4-hour poll] window several times over". At this repo's burst velocity that is false - 391
runs completed between 09:00 and 12:10 UTC that day - so most of a burst scrolled past
unrecorded and quietly thinned the sample check 5 is judged against.

No network: the gh layer is stubbed, so the fetch functions stay pure over their inputs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from scripts import ci_health_metrics as mod
from tests.scripts.test_ci_health_metrics import _job_item, _run

WATERMARK_TS = "2026-08-31T09:00:00Z"
WATERMARK = datetime(2026, 8, 31, 9, 0, 0, tzinfo=UTC)


def _stamp(offset_minutes: float) -> str:
    """A GitHub Z timestamp `offset_minutes` from the watermark. Negative is before it."""
    return (WATERMARK + timedelta(minutes=offset_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ci_record(ts: str, job_id: int) -> mod.Metric:
    return mod.Metric(ts=ts, step="ci:CI/pytest", seconds=30.0, source="ci", job_id=job_id)


def _run_item(run_id: int, *, created: str, updated: str, event: str = "push") -> dict:
    return {
        "id": run_id,
        "name": "CI",
        "event": event,
        "head_branch": "main",
        "conclusion": "success",
        "run_started_at": created,
        "updated_at": updated,
    }


class _FakeGh:
    """One stub serving both endpoints _fetch_jobs touches, keyed off the requested path.

    Records every path so a test can prove API economy as well as correctness: a quiet
    window must not touch the per-run jobs endpoint at all, and the page bound must
    actually stop the paging rather than merely truncating the result.

    Each run is given exactly one job whose job_id equals its run_id, so a test can point
    at a specific job without threading a second id through the fixture.
    """

    def __init__(self, pages: list[list[dict]], *, total_count: int | None = None) -> None:
        self.pages = pages
        self.total_count = (
            total_count if total_count is not None else sum(len(page) for page in pages)
        )
        self.run_paths: list[str] = []
        self.job_paths: list[str] = []

    def __call__(self, path: str) -> dict:
        if "/jobs?" in path:
            self.job_paths.append(path)
            run_id = int(path.split("/runs/")[1].split("/jobs", maxsplit=1)[0])
            return {
                "jobs": [
                    _job_item(
                        run_id,
                        conclusion="success",
                        started=_stamp(0),
                        completed=_stamp(0.5),
                        name="pytest",
                    )
                ]
            }
        self.run_paths.append(path)
        # '&page=' not 'page=': the query string also carries 'per_page='.
        page = int(path.split("&page=")[1].split("&", maxsplit=1)[0])
        items = self.pages[page - 1] if page <= len(self.pages) else []
        return {"total_count": self.total_count, "workflow_runs": items}


def _full_page(first_id: int) -> list[dict]:
    """RUN_FETCH_COUNT runs, all completed after the watermark - a page GitHub filled."""
    return [
        _run_item(first_id + index, created=_stamp(1), updated=_stamp(2))
        for index in range(mod.RUN_FETCH_COUNT)
    ]


def _stale_page(first_id: int) -> list[dict]:
    """RUN_FETCH_COUNT runs, all completed BEFORE the watermark - lookback overlap."""
    return [
        _run_item(first_id + index, created=_stamp(-120), updated=_stamp(-60))
        for index in range(mod.RUN_FETCH_COUNT)
    ]


def test_burst_larger_than_the_bootstrap_window_is_fully_recorded(monkeypatch):
    """If a burst of 30 runs since the watermark lost any of them
    then the fixed newest-10 window is still in place and the sample thins, or broken.
    """
    page = [
        _run_item(500 + index, created=_stamp(1), updated=_stamp(2 + index)) for index in range(30)
    ]
    fake = _FakeGh([page])
    monkeypatch.setattr(mod, "_gh_api_json", fake)

    outcome = mod._fetch_jobs([], [_ci_record(WATERMARK_TS, 1)])

    assert len(outcome.jobs) == 30
    assert {job.job_id for job in outcome.jobs} == {500 + index for index in range(30)}
    assert outcome.note is None
    assert len(fake.job_paths) == 30


def test_bootstrap_seeds_from_the_newest_runs_when_the_store_has_no_ci_records(monkeypatch):
    """If a fresh machine paged the whole listing
    then a first install would spend hundreds of API calls seeding history, or broken.
    """
    fake = _FakeGh([])
    monkeypatch.setattr(mod, "_gh_api_json", fake)
    runs = [
        _run(age_minutes=index + 1, duration_seconds=100, conclusion="success")
        for index in range(30)
    ]

    outcome = mod._fetch_jobs(runs, [])

    assert len(outcome.jobs) == mod.JOB_FETCH_BOOTSTRAP_RUN_COUNT
    assert outcome.note is None
    assert fake.run_paths == []
    assert len(fake.job_paths) == mod.JOB_FETCH_BOOTSTRAP_RUN_COUNT


def test_quiet_window_costs_no_per_run_job_calls(monkeypatch):
    """If runs already behind the watermark were re-fetched
    then every 4-hourly poll would pay for jobs it has already recorded, or broken.
    """
    page = [_run_item(600 + index, created=_stamp(-120), updated=_stamp(-60)) for index in range(5)]
    fake = _FakeGh([page])
    monkeypatch.setattr(mod, "_gh_api_json", fake)

    outcome = mod._fetch_jobs([], [_ci_record(WATERMARK_TS, 1)])

    assert outcome.jobs == []
    assert outcome.note is None
    assert fake.job_paths == []
    assert len(fake.run_paths) == 1


def test_catch_up_pages_past_a_full_first_page(monkeypatch):
    """If paging stopped at page 1 then a backlog deeper than one page would be lost."""
    tail = [
        _run_item(9001, created=_stamp(-30), updated=_stamp(1)),
        _run_item(9002, created=_stamp(-30), updated=_stamp(3)),
        _run_item(9003, created=_stamp(-600), updated=_stamp(-500)),
    ]
    fake = _FakeGh([_full_page(1000), tail])
    monkeypatch.setattr(mod, "_gh_api_json", fake)

    outcome = mod._fetch_jobs([], [_ci_record(WATERMARK_TS, 1)])

    assert len(fake.run_paths) == 2
    assert "page=2" in fake.run_paths[1]
    assert len(outcome.jobs) == mod.RUN_FETCH_COUNT + 2
    assert {9001, 9002} <= {job.job_id for job in outcome.jobs}
    assert 9003 not in {job.job_id for job in outcome.jobs}


def test_truncated_catch_up_records_what_it_got_and_names_the_gap(monkeypatch):
    """If the page bound were hit silently
    then a permanent hole in the sample would look identical to a healthy poll, or broken.
    """
    pages = [_full_page(10_000 + page * 1000) for page in range(mod.JOB_FETCH_MAX_PAGES)]
    fetchable = mod.JOB_FETCH_MAX_PAGES * mod.RUN_FETCH_COUNT
    fake = _FakeGh(pages, total_count=fetchable + 500)
    monkeypatch.setattr(mod, "_gh_api_json", fake)

    outcome = mod._fetch_jobs([], [_ci_record(WATERMARK_TS, 1)])

    assert len(outcome.jobs) == fetchable
    assert outcome.note is not None
    assert "500" in outcome.note
    assert str(mod.JOB_FETCH_MAX_PAGES) in outcome.note
    # No page beyond the bound was requested: the bound is real, not cosmetic.
    assert len(fake.run_paths) == mod.JOB_FETCH_MAX_PAGES


def test_truncation_is_loud_but_never_fatal(monkeypatch):
    """If the page bound raised
    then all five checks would abort every poll and never self-heal, or broken.
    """
    pages = [_full_page(20_000 + page * 1000) for page in range(mod.JOB_FETCH_MAX_PAGES)]
    monkeypatch.setattr(mod, "_gh_api_json", _FakeGh(pages, total_count=99_999))

    # No pytest.raises: reaching the assertion at all is the point of this case.
    assert mod._fetch_jobs([], [_ci_record(WATERMARK_TS, 1)]).note is not None


def test_backlog_of_exactly_the_page_bound_is_complete_not_a_gap(monkeypatch):
    """If exhausting the page bound alone fired the gap note
    then a backlog of exactly JOB_FETCH_MAX_PAGES full pages would claim a hole of zero
    runs in the log, or broken.
    """
    pages = [_full_page(30_000 + page * 1000) for page in range(mod.JOB_FETCH_MAX_PAGES)]
    # total_count defaults to exactly the fetched count: nothing was left behind.
    fake = _FakeGh(pages)
    monkeypatch.setattr(mod, "_gh_api_json", fake)

    outcome = mod._fetch_jobs([], [_ci_record(WATERMARK_TS, 1)])

    assert len(outcome.jobs) == mod.JOB_FETCH_MAX_PAGES * mod.RUN_FETCH_COUNT
    assert outcome.note is None


def test_busy_prior_day_past_the_bound_is_overlap_not_a_gap(monkeypatch):
    """If the gap note keyed on total_count alone
    then a busy day BEFORE the watermark could fill the created lookback past the page
    bound and claim a permanent hole on every poll, despite every run newer than the
    watermark having been fetched, or broken.
    """
    pages = [_full_page(40_000)] + [
        _stale_page(50_000 + page * 1000) for page in range(mod.JOB_FETCH_MAX_PAGES - 1)
    ]
    # 2000 more already-recorded prior-day runs sit beyond the bound.
    fake = _FakeGh(pages, total_count=mod.JOB_FETCH_MAX_PAGES * mod.RUN_FETCH_COUNT + 2000)
    monkeypatch.setattr(mod, "_gh_api_json", fake)

    outcome = mod._fetch_jobs([], [_ci_record(WATERMARK_TS, 1)])

    # Everything newer than the watermark (page 1) was recorded, and no gap is claimed:
    # the deepest page held nothing at or after the watermark, so the remainder is the
    # already-recorded prior day swept in by the created lookback.
    assert len(outcome.jobs) == mod.RUN_FETCH_COUNT
    assert outcome.note is None


def test_boundary_run_is_refetched_and_deduped_by_job_id(monkeypatch, tmp_path):
    """If the inclusive watermark boundary double-recorded its own run
    then the median would describe the polling cadence, not the build, or broken.
    """
    page = [_run_item(777, created=_stamp(-10), updated=_stamp(0))]
    monkeypatch.setattr(mod, "_gh_api_json", _FakeGh([page]))
    existing = [_ci_record(WATERMARK_TS, 777)]

    outcome = mod._fetch_jobs([], existing)

    assert [job.job_id for job in outcome.jobs] == [777]
    path = tmp_path / "metrics.jsonl"
    assert mod._record_job_metrics(outcome.jobs, existing, path) == 0
    assert not path.exists()


def test_local_records_never_advance_the_watermark():
    """If a local `just` timing set the watermark
    then every CI run older than the maintainer's last local build would be skipped, or broken.
    """
    existing = [
        _ci_record(WATERMARK_TS, 1),
        mod.Metric(ts=_stamp(600), step="just test", seconds=12.0, source="local", job_id=None),
    ]
    assert mod._ci_watermark(existing) == WATERMARK


def test_watermark_is_none_on_a_store_with_no_ci_records():
    """If a local-only store produced a watermark
    then a fresh machine would page the listing instead of bootstrapping, or broken.
    """
    local = mod.Metric(ts=_stamp(0), step="just test", seconds=12.0, source="local", job_id=None)
    assert mod._ci_watermark([local]) is None
    assert mod._ci_watermark([]) is None


def test_malformed_ts_on_a_ci_record_fails_loudly():
    """If an unparseable ci ts were skipped
    then the watermark would silently move by an unknown amount, or broken.
    """
    with pytest.raises(mod.PreconditionError) as excinfo:
        mod._ci_watermark([_ci_record("not-a-date", 4242)])
    assert "4242" in str(excinfo.value)
    assert "ts" in str(excinfo.value)


def test_long_running_run_created_before_the_watermark_is_included(monkeypatch):
    """If selection used the listing's created/started order rather than updated_at
    then a run that started early and finished late would never be recorded, or broken.
    """
    page = [_run_item(8080, created=_stamp(-120), updated=_stamp(5))]
    fake = _FakeGh([page])
    monkeypatch.setattr(mod, "_gh_api_json", fake)

    outcome = mod._fetch_jobs([], [_ci_record(WATERMARK_TS, 1)])

    assert [job.job_id for job in outcome.jobs] == [8080]
    # The listing is bounded by `created`, so the lookback has to reach back far enough to
    # still contain a run that started long before it completed.
    lookback = (WATERMARK - timedelta(hours=mod.JOB_FETCH_CREATED_LOOKBACK_HOURS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    assert f"created=%3E%3D{lookback}" in fake.run_paths[0]
