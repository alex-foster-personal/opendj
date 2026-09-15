"""Jobs runner loop - operational-plan section 12, R3/R4.

The UI's right-click already records intent in the queue (POST
/api/v1/lyrics/jobs -> data/state/lyrics-jobs.json). This module is the
consumer: it takes the OLDEST queued job, invokes the R1 batch driver
(apps/lyrics/batch.py) for the job's stable_ids, writes stage progress into
the job's note as it goes (R4's honest progress - the admin jobs panel reads
the same note field), and marks the job done/failed with the driver report.

``work_once`` is the testable + launchd-able unit (`just lyrics-jobs-worker`
loops it). A StageBlocked or failed stage command marks the job failed with
the blocker named and the loop continues to the next poll; any OTHER
exception still marks the job failed but then re-raises, because a
programming error must crash loudly (brittle fail-fast house rule).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from apps.lyrics import jobs as jobs_mod
from apps.lyrics.batch import BatchReport, CommandFailed, StageBlocked, batch_paths_for, run_batch
from apps.shared.paths import PROJECT_ROOT, STATE_DIR

DEFAULT_STATE_DIR: Path = STATE_DIR
POLL_SLEEP_S: float = 30.0
NOTE_MAX_CHARS: int = 2000  # the queue file is UI-facing state, not a log


class Driver(Protocol):
    def __call__(self, *, corpus: str, stable_ids: list[str], live: bool,
                 progress: Callable[[str], None]) -> BatchReport: ...


def work_once(state_dir: Path, *, driver: Driver | None = None) -> bool:
    """Consume ONE queued job (oldest first). Returns False when idle."""
    queued = [j for j in jobs_mod.list_jobs(state_dir) if j.status == "queued"]
    if not queued:
        print("[OK] queue empty - idle")
        return False
    job = queued[0]
    corpus = f"job-{job.id}"
    print(f"[..] job {job.id} ({job.kind}, {len(job.stable_ids)} track(s)) "
          f"-> corpus {corpus}")
    progress_lines: list[str] = []

    def progress(msg: str) -> None:
        progress_lines.append(msg.splitlines()[0])
        note = " | ".join(progress_lines)[-NOTE_MAX_CHARS:]
        jobs_mod.annotate(state_dir, job.id, note)
        print(f"[batch] {msg}")

    if driver is None:
        paths = batch_paths_for(state_dir)

        def driver(*, corpus: str, stable_ids: list[str], live: bool,
                   progress: Callable[[str], None]) -> BatchReport:
            return run_batch(
                corpus=corpus,
                stable_ids=stable_ids,
                live=live,
                progress=progress,
                paths=paths,
                repo_root=PROJECT_ROOT,
            )

    try:
        report = driver(corpus=corpus, stable_ids=list(job.stable_ids),
                        live=True, progress=progress)
    except (StageBlocked, CommandFailed) as exc:
        jobs_mod.complete(state_dir, job.id, "failed", f"blocked: {exc}")
        print(f"[ERROR] job {job.id} failed: {exc}")
        return True
    except Exception as exc:
        jobs_mod.complete(state_dir, job.id, "failed",
                          f"{type(exc).__name__}: {exc}")
        raise
    jobs_mod.complete(state_dir, job.id, "done",
                      report.summary()[:NOTE_MAX_CHARS])
    print(f"[OK] job {job.id} done: aligned {len(report.aligned)}, "
          f"ingested {report.ingested}, excluded {len(report.excluded)}")
    return True


def work_loop(state_dir: Path, *, driver: Driver | None = None,
              poll_sleep_s: float = POLL_SLEEP_S) -> None:
    """Consume forever with a modest poll sleep (R3's runner-up guarantee:
    a queued job never sits unconsumed while this loop is alive)."""
    print(f"[..] lyrics jobs worker up (poll {poll_sleep_s:.0f}s, "
          f"queue {jobs_mod.jobs_path(state_dir)})")
    while True:
        worked = work_once(state_dir, driver=driver)
        if not worked:
            time.sleep(poll_sleep_s)
