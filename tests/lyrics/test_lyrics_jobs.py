"""The offline-runner job queue: a JSON file with one writer per side.

- if enqueue accepts an unknown kind then a runner drains a job it cannot
  execute and the queue silently rots -- broken
- if a completed job can be annotated or completed again then the record of
  what the runner reported is editable after the fact -- broken
- if list_jobs raises on a missing file then a fresh install cannot open the
  admin panel; an absent queue is EMPTY, not an error -- broken
- if complete(note=None) wipes the note the runner just annotated then the
  stage progress is lost at exactly the moment it matters -- broken
"""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.lyrics import jobs


def test_a_missing_queue_file_lists_as_empty(tmp_path: Path) -> None:
    assert jobs.list_jobs(tmp_path) == []


def test_enqueue_list_annotate_complete_round_trip(tmp_path: Path) -> None:
    queued = jobs.enqueue(tmp_path, "lyricsync", ["sid-1", "sid-2"], "from the UI")
    assert queued.status == "queued"
    assert jobs.list_jobs(tmp_path) == [queued]

    annotated = jobs.annotate(tmp_path, queued.id, "aligning 1/2")
    assert annotated.note == "aligning 1/2"
    assert annotated.status == "queued"

    done = jobs.complete(tmp_path, queued.id, "done")
    assert (done.status, done.note) == ("done", "aligning 1/2"), (
        "complete(note=None) keeps the annotation the runner already made"
    )
    assert jobs.list_jobs(tmp_path) == [done]
    assert jobs.jobs_path(tmp_path).is_file()


def test_enqueue_rejects_an_unknown_kind(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown job kind 'transcode'"):
        jobs.enqueue(tmp_path, "transcode", ["sid-1"])


def test_enqueue_rejects_an_empty_or_duplicated_track_list(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least one stable_id"):
        jobs.enqueue(tmp_path, "stems", [])
    with pytest.raises(ValueError, match="duplicate stable_ids"):
        jobs.enqueue(tmp_path, "stems", ["sid-1", "sid-1"])


def test_complete_rejects_a_status_that_is_not_a_completion(tmp_path: Path) -> None:
    job = jobs.enqueue(tmp_path, "analyze", ["sid-1"])
    with pytest.raises(ValueError, match="must be done\\|failed"):
        jobs.complete(tmp_path, job.id, "queued")


def test_a_finished_job_refuses_late_edits(tmp_path: Path) -> None:
    job = jobs.enqueue(tmp_path, "analyze", ["sid-1"])
    jobs.complete(tmp_path, job.id, "failed", "no audio")
    with pytest.raises(ValueError, match="already failed"):
        jobs.annotate(tmp_path, job.id, "second thoughts")
    with pytest.raises(ValueError, match="already failed"):
        jobs.complete(tmp_path, job.id, "done")


def test_an_unknown_job_id_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no job with id 'nope'"):
        jobs.complete(tmp_path, "nope", "done")


def test_a_queue_file_that_is_not_a_list_raises(tmp_path: Path) -> None:
    jobs.jobs_path(tmp_path).write_text('{"id": "x"}', encoding="utf-8")
    with pytest.raises(TypeError, match="must hold a JSON list"):
        jobs.list_jobs(tmp_path)
