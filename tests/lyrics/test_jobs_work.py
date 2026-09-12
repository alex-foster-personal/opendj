"""jobs runner loop (operational-plan section 12, R3/R4): queue -> R1 driver.

Regression lines (one-line if/then, house format):
- if `jobs work --once` with an empty queue does not exit cleanly (idle, code
  0) then the launchd unit flaps - broken
- if a queued job processed by a succeeding driver is not marked done with the
  driver report as its note then broken
- if a StageBlocked driver marks the job anything but failed-with-the-blocker
  then broken
- if stage progress stops being written into the job note while the driver
  runs (R4's honest progress) then broken
- if annotate() ever touches a completed job then the runner can rewrite
  history - broken
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.lyrics import jobs as jobs_mod
from apps.lyrics.batch import BatchReport, StageBlocked
from apps.lyrics.worker import work_once


@pytest.fixture()
def state_dir(tmp_path: Path) -> Path:
    return tmp_path / "state"


def _queue(state_dir: Path, stable_ids: list[str]) -> jobs_mod.LyricJob:
    return jobs_mod.enqueue(state_dir, "lyricsync", stable_ids)


def _job(state_dir: Path, job_id: str) -> jobs_mod.LyricJob:
    return next(j for j in jobs_mod.list_jobs(state_dir) if j.id == job_id)


#-----------------------------------------------------------------------------


def test_empty_queue_is_a_clean_idle_exit(state_dir: Path) -> None:
    assert work_once(state_dir, driver=lambda **_kw: pytest.fail("must not run")) is False


def test_succeeding_driver_marks_done_with_report_note(state_dir: Path) -> None:
    job = _queue(state_dir, ["sid-a", "sid-b"])
    seen: dict = {}

    def driver(*, corpus: str, stable_ids: list[str], progress, **_kw) -> BatchReport:
        seen.update(corpus=corpus, stable_ids=stable_ids)
        progress("align: 2/2 tracks")
        assert "align: 2/2 tracks" in (_job(state_dir, job.id).note or "")
        report = BatchReport(corpus=corpus, live=True)
        report.aligned = ["t000", "t001"]
        report.ingested = 2
        return report

    assert work_once(state_dir, driver=driver) is True
    assert seen == {"corpus": f"job-{job.id}", "stable_ids": ["sid-a", "sid-b"]}
    done = _job(state_dir, job.id)
    assert done.status == "done"
    assert done.note is not None and "aligned 2" in done.note


def test_blocked_driver_marks_failed_naming_the_blocker(state_dir: Path) -> None:
    job = _queue(state_dir, ["sid-a"])

    def driver(**_kw) -> BatchReport:
        raise StageBlocked("stems: Modal unreachable")

    assert work_once(state_dir, driver=driver) is True
    failed = _job(state_dir, job.id)
    assert failed.status == "failed"
    assert failed.note is not None and "Modal unreachable" in failed.note


def test_oldest_queued_job_goes_first(state_dir: Path) -> None:
    first = _queue(state_dir, ["sid-1"])
    _queue(state_dir, ["sid-2"])
    handled: list[str] = []

    def driver(*, corpus: str, **_kw) -> BatchReport:
        handled.append(corpus)
        return BatchReport(corpus=corpus, live=True)

    work_once(state_dir, driver=driver)
    assert handled == [f"job-{first.id}"]
    assert _job(state_dir, first.id).status == "done"


def test_annotate_updates_only_queued_jobs(state_dir: Path) -> None:
    job = _queue(state_dir, ["sid-a"])
    jobs_mod.annotate(state_dir, job.id, "stems: 1/1")
    assert _job(state_dir, job.id).note == "stems: 1/1"
    jobs_mod.complete(state_dir, job.id, "done", "final")
    with pytest.raises(ValueError, match="already done"):
        jobs_mod.annotate(state_dir, job.id, "late scribble")
