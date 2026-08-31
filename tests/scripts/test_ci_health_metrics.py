"""Tests for :mod:`scripts.ci_health_metrics`: the shared store, CI job recording, check 5.

Every case here is one of the natural-language acceptance tests written into that module's
mini-PRD.

Two of them are not hypotheticals. The FIRST live run of check 5 against this repo recorded
'ci:CI/quality ratchet' at -1.0s and 'ci:CI/Windows Rekordbox parity gate' at -11.0s,
because GitHub stamps a skipped job's completed_at one to eleven seconds BEFORE its
started_at. Left alone, negative numbers would have sat in the median every future run is
judged against. The skipped-job and negative-duration cases below pin that fix.

No network: the gh layer is stubbed, so the check functions stay pure over their inputs.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from scripts import ci_health_metrics as mod
from scripts.ci_health_core import EXIT_ITERATION_SPEED

NOW = datetime(2026, 8, 19, 20, 0, 0, tzinfo=UTC)


def _run(
    *,
    age_minutes: int,
    duration_seconds: float,
    conclusion: str,
    branch: str = "main",
    event: str = "push",
) -> mod.Run:
    started = NOW - timedelta(minutes=age_minutes)
    return mod.Run(
        run_id=age_minutes,
        name="CI",
        event=event,
        head_branch=branch,
        conclusion=conclusion,
        started_at=started,
        updated_at=started + timedelta(seconds=duration_seconds),
    )


# ----- R7 iteration-speed regression ------------------------------------------------


def _metric(step: str, seconds: float, *, job_id: int | None = None) -> mod.Metric:
    return mod.Metric(
        ts="2026-08-20T00:00:00Z", step=step, seconds=seconds, source="ci", job_id=job_id
    )


def _history(step: str, seconds: float, count: int) -> list[mod.Metric]:
    return [_metric(step, seconds) for _ in range(count)]


def _series(step: str, seconds: float, count: int, newest: float) -> list[mod.Metric]:
    """`count` baseline records at `seconds`, then a full recent window at `newest`.

    The check medians ITERATION_SPEED_RECENT_WINDOW newest records, so a fixture has to
    fill that window: a single trailing record is noise by design and must not fire.
    """
    return [
        *_history(step, seconds, count),
        *_history(step, newest, mod.ITERATION_SPEED_RECENT_WINDOW),
    ]


def test_iteration_speed_fires_when_a_step_doubles():
    """If a step's newest run took 40s against a 20s median then exit 6, or broken."""
    metrics = _series("ci:CI/pytest", 20.0, 20, 40.0)
    result = mod._check_iteration_speed(metrics)
    assert not result.ok
    assert result.classification == "iteration-speed"
    assert result.exit_code == EXIT_ITERATION_SPEED
    assert result.sample_size == 20
    assert "2.00x" in result.detail


def test_iteration_speed_quiet_just_under_the_factor():
    """If the newest run is 1.25x the median then that is under 1.5x, or broken."""
    metrics = _series("ci:CI/pytest", 20.0, 20, 25.0)
    assert mod._check_iteration_speed(metrics).ok


def test_iteration_speed_fires_just_over_the_factor():
    """If the newest run is 1.6x the median then the boundary is live, or broken."""
    metrics = _series("ci:CI/pytest", 20.0, 20, 32.0)
    assert not mod._check_iteration_speed(metrics).ok


def test_recent_records_are_excluded_from_the_baseline_median():
    """If the recent samples were inside their own baseline
    then a regression would blunt the median it is measured against, or broken.
    """
    # 5 baseline records at 20s. Including the 200s recent window would drag the baseline
    # upward and could hide the very shift being looked for.
    result = mod._check_iteration_speed(_series("ci:CI/pytest", 20.0, 5, 200.0))
    assert not result.ok
    assert "20.0s median" in result.detail
    assert result.sample_size == 5


def test_a_single_slow_run_does_not_fire():
    """If one unlucky run could page the maintainer then the check reports noise, not regressions.

    This is the real false positive that shaped the design: the first live run flagged
    'CI Cost Guard' at 39s against a 14s median, from one queue-bound outlier.
    """
    metrics = [*_history("ci:CI/pytest", 20.0, 20), _metric("ci:CI/pytest", 200.0)]
    assert mod._check_iteration_speed(metrics).ok


def test_a_persistent_shift_does_fire():
    """If a slowdown holding across the recent window stayed quiet then nothing is caught."""
    result = mod._check_iteration_speed(_series("ci:CI/pytest", 20.0, 20, 40.0))
    assert not result.ok
    assert result.classification == "iteration-speed"


def test_iteration_speed_ignores_steps_with_too_little_history():
    """If a step has fewer than ITERATION_SPEED_MIN_SAMPLE baseline records
    then no ratio is computable and it must not alert, or broken.
    """
    metrics = _series("ci:CI/pytest", 20.0, 2, 400.0)
    result = mod._check_iteration_speed(metrics)
    assert result.ok
    assert result.classification == "insufficient-data"


def test_thin_history_is_reported_not_silently_passed():
    """If insufficient data produced an empty detail
    then the shortfall would be invisible in the verdict line, or broken.
    """
    result = mod._check_iteration_speed(_history("ci:CI/pytest", 20.0, 2))
    assert str(mod.ITERATION_SPEED_MIN_SAMPLE) in result.detail
    assert "no regression is computable" in result.detail


def test_fast_steps_are_never_judged():
    """If a 2s step tripling to 6s alerted then scheduler jitter would page the maintainer, or broken."""
    metrics = _series("vite-hmr", 2.0, 20, 6.0)
    result = mod._check_iteration_speed(metrics)
    assert result.ok
    assert result.classification == "insufficient-data"


def test_median_window_ignores_records_older_than_the_window():
    """If more than ITERATION_SPEED_MEDIAN_WINDOW records fed the median
    then ancient timings would anchor it forever, or broken.
    """
    ancient = _history("ci:CI/pytest", 1000.0, 40)
    recent = _history("ci:CI/pytest", 20.0, 20)
    result = mod._check_iteration_speed(
        [*ancient, *recent, *_history("ci:CI/pytest", 40.0, mod.ITERATION_SPEED_RECENT_WINDOW)]
    )
    assert not result.ok
    assert result.sample_size == mod.ITERATION_SPEED_MEDIAN_WINDOW


def test_worst_offender_is_the_one_reported():
    """If a milder regression outranked a worse one then the alert would misdirect, or broken."""
    metrics = [
        *_series("ci:CI/mild", 20.0, 20, 34.0),
        *_series("ci:CI/severe", 20.0, 20, 100.0),
    ]
    result = mod._check_iteration_speed(metrics)
    assert not result.ok
    assert "ci:CI/severe" in result.detail


def test_healthy_verdict_names_how_many_steps_were_judged():
    """If the healthy detail hid its denominator then the rate would be unattributable."""
    metrics = _series("ci:CI/pytest", 20.0, 20, 21.0)
    result = mod._check_iteration_speed(metrics)
    assert result.ok
    assert result.classification == "healthy"
    assert "1 of 1 steps" in result.detail


# ----- metrics store parsing --------------------------------------------------------


def test_absent_store_is_not_an_error(tmp_path):
    """If a machine that has never timed anything raised
    then the watchdog would fail on first install, or broken.
    """
    assert mod._read_metrics(tmp_path / "nothing-here.jsonl") == []


def test_malformed_store_line_fails_loudly(tmp_path):
    """If a corrupt line were skipped then the median would silently drift, or broken."""
    path = tmp_path / "metrics.jsonl"
    path.write_text('{"ts":"t","step":"a","seconds":1}\nnot json at all\n')
    with pytest.raises(mod.PreconditionError) as excinfo:
        mod._read_metrics(path)
    assert "metrics.jsonl:2" in str(excinfo.value)


def test_store_line_missing_required_keys_fails_loudly(tmp_path):
    """If a line without seconds were accepted then it would break the median, or broken."""
    path = tmp_path / "metrics.jsonl"
    path.write_text('{"ts":"t","step":"a"}\n')
    with pytest.raises(mod.PreconditionError) as excinfo:
        mod._read_metrics(path)
    assert "seconds" in str(excinfo.value)


def test_non_numeric_seconds_fails_loudly(tmp_path):
    """If seconds were a string then statistics.median would explode later, or broken."""
    path = tmp_path / "metrics.jsonl"
    path.write_text('{"ts":"t","step":"a","seconds":"quick"}\n')
    with pytest.raises(mod.PreconditionError):
        mod._read_metrics(path)


def test_blank_lines_are_tolerated(tmp_path):
    """If a trailing newline raised then every well-formed store would fail, or broken."""
    path = tmp_path / "metrics.jsonl"
    path.write_text('{"ts":"t","step":"a","seconds":1}\n\n')
    assert len(mod._read_metrics(path)) == 1


# ----- CI job recording -------------------------------------------------------------


def _job(job_id: int, *, seconds: float, name: str = "pytest") -> mod.Job:
    started = NOW - timedelta(seconds=seconds)
    return mod.Job(
        job_id=job_id,
        workflow="CI",
        name=name,
        head_sha="abc1234",
        conclusion="success",
        started_at=started,
        completed_at=started + timedelta(seconds=seconds),
    )


def test_job_step_key_is_namespaced_away_from_local_steps():
    """If a CI job shared a key with a local `just` step
    then two different machines' timings would share one median, or broken.
    """
    assert _job(1, seconds=10).step == "ci:CI/pytest"


def test_recording_appends_new_jobs(tmp_path):
    """If new jobs were not written then check 5 would have nothing to read, or broken."""
    path = tmp_path / "metrics.jsonl"
    written = mod._record_job_metrics([_job(1, seconds=30), _job(2, seconds=40)], [], path)
    assert written == 2
    records = mod._read_metrics(path)
    assert [record.seconds for record in records] == [30.0, 40.0]
    assert all(record.source == mod.CI_METRIC_SOURCE for record in records)


def test_recording_skips_jobs_already_in_the_store(tmp_path):
    """If the 4-hourly poll re-recorded the same job
    then the median would describe the cadence, not the build, or broken.
    """
    path = tmp_path / "metrics.jsonl"
    mod._record_job_metrics([_job(1, seconds=30)], [], path)
    existing = mod._read_metrics(path)
    written = mod._record_job_metrics([_job(1, seconds=30), _job(2, seconds=40)], existing, path)
    assert written == 1
    assert len(mod._read_metrics(path)) == 2


def test_recording_is_a_no_op_when_everything_is_known(tmp_path):
    """If a fully-seen batch still opened the file then empty writes would churn it."""
    path = tmp_path / "metrics.jsonl"
    existing = [_metric("ci:CI/pytest", 30.0, job_id=1)]
    assert mod._record_job_metrics([_job(1, seconds=30)], existing, path) == 0
    assert not path.exists()


def test_failed_job_is_recorded_with_a_nonzero_exit(tmp_path):
    """If a failed job were filed as exit 0 then slow and broken would be indistinguishable."""
    path = tmp_path / "metrics.jsonl"
    job = _job(1, seconds=30)
    job.conclusion = "failure"
    mod._record_job_metrics([job], [], path)
    assert json.loads(path.read_text().splitlines()[0])["exit"] == 1


# ----- skipped jobs and negative durations ------------------------------------------
#
# Found live Thu 20 Aug 2026: the first real run of check 5 recorded 'ci:CI/quality
# ratchet' at -1.0s and 'ci:CI/Windows Rekordbox parity gate' at -11.0s. Both were
# conclusion 'skipped', and GitHub stamps a skipped job's completed_at one to eleven
# seconds BEFORE its started_at. Left alone, negative numbers would sit in the median that
# every future run is judged against.


def _jobs_payload(monkeypatch, items):
    """Stub the gh layer so _fetch_jobs runs over a literal jobs payload.

    These cases all run the BOOTSTRAP path (`existing=[]`, no watermark), so the run list
    returned here is the one _fetch_jobs works from and no runs listing is requested.
    """
    run = _run(age_minutes=1, duration_seconds=100, conclusion="success")
    monkeypatch.setattr(mod, "_gh_api_json", lambda path: {"jobs": items})
    return [run]


def _job_item(job_id, *, conclusion, started, completed, name="quality ratchet"):
    return {
        "id": job_id,
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
        "head_sha": "abc1234def",
        "started_at": started,
        "completed_at": completed,
    }


def test_skipped_jobs_are_never_recorded(monkeypatch):
    """If a skipped job entered the store then a negative duration would poison the median."""
    runs = _jobs_payload(
        monkeypatch,
        [
            _job_item(
                1,
                conclusion="skipped",
                started="2026-08-20T18:21:09Z",
                completed="2026-08-20T18:21:08Z",
            )
        ],
    )
    assert mod._fetch_jobs(runs, []).jobs == []


@pytest.mark.parametrize("conclusion", ["skipped", "cancelled", "neutral"])
def test_non_executing_conclusions_are_all_excluded(monkeypatch, conclusion):
    """If a job that never ran were timed then the number would describe nothing."""
    runs = _jobs_payload(
        monkeypatch,
        [
            _job_item(
                1,
                conclusion=conclusion,
                started="2026-08-20T18:00:00Z",
                completed="2026-08-20T18:00:30Z",
            )
        ],
    )
    assert mod._fetch_jobs(runs, []).jobs == []


def test_executed_jobs_are_still_recorded(monkeypatch):
    """If the skip filter also dropped real jobs then check 5 would starve, or broken."""
    runs = _jobs_payload(
        monkeypatch,
        [
            _job_item(
                1,
                conclusion="success",
                started="2026-08-20T18:00:00Z",
                completed="2026-08-20T18:00:30Z",
            )
        ],
    )
    jobs = mod._fetch_jobs(runs, []).jobs
    assert len(jobs) == 1
    assert jobs[0].duration_seconds == 30.0


def test_negative_duration_on_an_executed_job_fails_loudly(monkeypatch):
    """If an unexplained negative duration were dropped quietly
    then a change in GitHub's timing semantics would hide behind a thinning sample.
    """
    runs = _jobs_payload(
        monkeypatch,
        [
            _job_item(
                77,
                conclusion="success",
                started="2026-08-20T18:00:30Z",
                completed="2026-08-20T18:00:00Z",
            )
        ],
    )
    with pytest.raises(mod.PreconditionError) as excinfo:
        mod._fetch_jobs(runs, [])
    assert "negative duration" in str(excinfo.value)
    assert "77" in str(excinfo.value)


def test_incomplete_jobs_are_skipped(monkeypatch):
    """If an in-progress job were timed then a half-finished run would set the median."""
    item = _job_item(
        1, conclusion="success", started="2026-08-20T18:00:00Z", completed="2026-08-20T18:00:30Z"
    )
    item["status"] = "in_progress"
    item["completed_at"] = None
    assert mod._fetch_jobs(_jobs_payload(monkeypatch, [item]), []).jobs == []


# ----- R4 watermark catch-up --------------------------------------------------------
#
# The incident this section pins, Sun 31 Aug 2026: _fetch_jobs took a fixed newest-10
# slice of the runs checks 1-4 had already fetched, on the documented assumption that 10
# "covers the [4-hour poll] window several times over". At this repo's burst velocity that
# is false - 391 runs completed between 09:00 and 12:10 UTC that day - so most of a burst
# scrolled past unrecorded and quietly thinned the sample check 5 is judged against.

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
