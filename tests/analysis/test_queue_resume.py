"""Cancel and resume the backfill queue, in one process and across a kill.

Lane brief `(private path)` item 4, requirement
NATIVE-10 ("[if] a backfill is cancelled and restarted [then] it resumes and
re-runs idempotently, reporting progress throughout").

[if] a backfill is cancelled and restarted [then] it resumes and reruns idempotently, [else stop].

TWO ARMS, and the brief is explicit that only the second one proves the
contract:

* arm (a) cancels a live batch and resumes it in the SAME process. An
  entirely in-memory queue passes this, so it is necessary and not
  sufficient.
* arm (b) KILLS the worker process with SIGKILL mid-batch and starts a FRESH
  process against the persisted queue state. Items completed before the kill
  must be no-ops on the fresh process (not re-run, not double-counted) while
  pending and in-flight-at-kill items resume and complete EXACTLY ONCE.

Counting real executions, not rows: the record write is idempotent on
(stable_id, backend, backend_version), so a second run of a completed track
leaves the row identical and is invisible in the database. The probe backends
write one marker file per ACTUAL analyze() call, so a double-run shows up as
two markers for one track. Without that, "no double work" would be an
absence claim with no instrument behind it.

Single-line intent, one assertion block each:
- if a cancel leaves an in-flight item marked done with no record behind it,
  the queue has a torn record -- broken.
- if a resume re-runs a completed track, a resumed backfill costs more than
  the work it has left -- broken.
- if a fresh process cannot see what the killed one committed, the queue is
  not persistent and the NATIVE-10 line is unproven -- broken.
- if an item the killed process was holding is lost rather than re-run, a
  kill silently drops work -- broken.
- if a second enqueue of an already-analyzed track runs it again, the
  idempotent-re-run promise is decoration -- broken.
- if a worker that finishes AFTER a live cancel settles its item back to
  done, cancellation does not cancel and resume never revisits the item --
  broken.
- if a backend-wide availability failure is written as a per-track `failed`,
  installing the missing capability and resuming cannot recover the batch --
  broken.

Public entry point: re-exports symbols other modules import.
"""

from __future__ import annotations

from tests.analysis.queue_resume_support import (
    AMPLE_HOST,
    REPO_ROOT,
    _candidates,
    _kill_group,
    _marker_counts,
    _spawn_runner,
    probe_dir,
)

__all__ = [
    "AMPLE_HOST",
    "REPO_ROOT",
    "_candidates",
    "_kill_group",
    "_marker_counts",
    "_spawn_runner",
    "probe_dir",
]
