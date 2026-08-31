"""Tests for :mod:`scripts.ci_health_metrics`: the shared store, CI job recording, check 5.

Every case here is one of the natural-language acceptance tests written into that module's
mini-PRD.

Two of them are not hypotheticals. The FIRST live run of check 5 against this repo recorded
'ci:CI/quality ratchet' at -1.0s and 'ci:CI/Windows Rekordbox parity gate' at -11.0s,
because GitHub stamps a skipped job's completed_at one to eleven seconds BEFORE its
started_at. Left alone, negative numbers would have sat in the median every future run is
judged against. The skipped-job and negative-duration cases below pin that fix.

No network: the gh layer is stubbed, so the check functions stay pure over their inputs.

The R4 watermark catch-up cases live in test_ci_health_watermark.py, split out to keep both
files under the repo's 600-line quality gate.
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


def test_check_windows_in_ts_order_not_append_order():
    """If check 5 trusted append order
    then a catch-up that appended an older-completed job after a newer record would judge
    stale samples as the newest ones, or broken.
    """

    def _at(minute: int, seconds: float) -> mod.Metric:
        return mod.Metric(
            ts=f"2026-08-20T00:{minute:02d}:00Z",
            step="ci:CI/pytest",
            seconds=seconds,
            source="ci",
            job_id=None,
        )

    slow_newest = [_at(50 + index, 40.0) for index in range(mod.ITERATION_SPEED_RECENT_WINDOW)]
    baseline = [_at(index, 20.0) for index in range(20)]
    # Append order puts the newest-by-ts records FIRST, as a straddling-run catch-up can.
    result = mod._check_iteration_speed([*slow_newest, *baseline])
    assert not result.ok
    assert "2.00x" in result.detail


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
