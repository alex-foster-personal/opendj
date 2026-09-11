"""Lyric/stem processing job queue - the honest bridge between UI intent
and offline runners.

The heavy work (demucs stems, MMS alignment, ASR) runs in PEP 723 scripts
on Modal/the farm, never in the daemon (house rule). So "Generate LyricSync"
in a context menu cannot BE the work - it records the request. This queue is
that record: a JSON file the daemon appends to and the runner scripts drain.
The UI states this plainly ("queued for the offline runner"), which keeps it
inside the no-mocked-behaviour rule: queueing is real state with a real
consumer, not a pretend action.

One writer per side: the daemon enqueues (POST /api/v1/lyrics/jobs), runners
complete (python -m apps.lyrics jobs complete <id> --status done|failed).
Both go through this module; nothing else writes the file.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

JOB_KINDS: frozenset[str] = frozenset({"analyze", "lyricsync", "stems"})
JOB_STATUSES: frozenset[str] = frozenset({"queued", "done", "failed"})


@dataclass(frozen=True)
class LyricJob:
    id: str
    ts: str
    kind: str
    stable_ids: list[str]
    status: str
    note: str | None


def jobs_path(state_dir: Path) -> Path:
    """``state_dir`` is the directory holding state.db (data/state)."""
    return state_dir / "lyrics-jobs.json"


def _load(path: Path) -> list[LyricJob]:
    if not path.is_file():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{path} must hold a JSON list")
    return [LyricJob(**entry) for entry in raw]


def _save(path: Path, jobs: list[LyricJob]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([asdict(j) for j in jobs], indent=2) + "\n", encoding="utf-8"
    )


def list_jobs(state_dir: Path) -> list[LyricJob]:
    return _load(jobs_path(state_dir))


def enqueue(state_dir: Path, kind: str, stable_ids: list[str], note: str | None = None) -> LyricJob:
    if kind not in JOB_KINDS:
        raise ValueError(f"unknown job kind {kind!r}; valid: {sorted(JOB_KINDS)}")
    if not stable_ids:
        raise ValueError("a job needs at least one stable_id")
    if len(set(stable_ids)) != len(stable_ids):
        raise ValueError("duplicate stable_ids in one job")
    path = jobs_path(state_dir)
    jobs = _load(path)
    job = LyricJob(
        id=uuid.uuid4().hex[:12],
        ts=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        kind=kind,
        stable_ids=list(stable_ids),
        status="queued",
        note=note,
    )
    jobs.append(job)
    _save(path, jobs)
    return job


def annotate(state_dir: Path, job_id: str, note: str) -> LyricJob:
    """Update a QUEUED job's note in place - the runner's live stage progress
    (section-12 R4). Completion still goes through complete(); a job that has
    already finished refuses late scribbles."""
    path = jobs_path(state_dir)
    jobs = _load(path)
    for i, job in enumerate(jobs):
        if job.id == job_id:
            if job.status != "queued":
                raise ValueError(f"job {job_id} is already {job.status}")
            updated = LyricJob(**{**asdict(job), "note": note})
            jobs[i] = updated
            _save(path, jobs)
            return updated
    raise ValueError(f"no job with id {job_id!r}")


def complete(state_dir: Path, job_id: str, status: str, note: str | None = None) -> LyricJob:
    if status not in JOB_STATUSES - {"queued"}:
        raise ValueError(f"completion status must be done|failed, got {status!r}")
    path = jobs_path(state_dir)
    jobs = _load(path)
    for i, job in enumerate(jobs):
        if job.id == job_id:
            if job.status != "queued":
                raise ValueError(f"job {job_id} is already {job.status}")
            updated = LyricJob(**{**asdict(job), "status": status, "note": note or job.note})
            jobs[i] = updated
            _save(path, jobs)
            return updated
    raise ValueError(f"no job with id {job_id!r}")
