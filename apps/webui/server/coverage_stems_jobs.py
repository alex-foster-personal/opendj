"""A failed user-started stems job, written into the coverage outcome ledger.

The enrich card names failed separations from ``coverage.failed.stems``
(ENRICH-04), and that count is read from the outcome ledger. The coverage
drain is the ledger's other failure writer, and it never runs stems (its
``JOB_ORDER`` leaves stems out on purpose), so a ``POST /api/v1/jobs``
``stems.separate`` run that failed was recorded only in the jobs store, and
the card went on reading "nothing failed".

:func:`record_stems_job_failure` is the engine's finish observer for that job
kind (wired in ``apps.engine_core.app``). On ``failed`` it records one ledger
failure per requested track that still has no bundle, keyed on the same
audio signature the coverage reading compares against. It lives here, not in
``apps.stems``, because the ledger belongs to the web UI layer and a domain
package must not import it.

What it skips, and why:

* any status but ``failed``: a success has nothing to record, and the runner
  never reports a cancel (the caller's verdict, not the work's).
* a track whose bundle landed before the run died: it did not fail.
* a scope job (``scope: pending``, what the enrich card's own button sends)
  resolves its targets inside the worker, and nothing outside the worker
  knows that set. Re-resolving "pending" afterwards does not: a track that
  came back from an unmounted volume mid-run, or lost its bundle, is pending
  now and was never attempted. So a scope job is attributed ONLY to the
  tracks the worker itself named in its ``failures:`` report (both stems
  workers print one, ``<stable_id>: <error>`` per line, and the runner keeps
  it in the job's error tail). A track the worker never named, because it
  crashed first or the tail had rolled past it, gets no per-track outcome:
  the failed job row stays the only, unattributed record of that run.
* a track whose audio does not resolve on this machine: there is no
  signature to key on, and the coverage reading does not count it either.

The failure is recorded TERMINAL (``MAX_ATTEMPTS`` at once): nothing retries a
user-started stems run on its own, so a backoff would only hide it. A later
run that lands the bundle takes the track out of ``missing``, which is all the
reading counts failures over; replacing the audio file re-arms it.

Requirements (mini-PRD):
  ✔︎ ✅ 🎯 ENRICH-04 a failed stems job reaches failed.stems
    [if] the job failed and a target has no bundle [then] a terminal failure
    [if] a target's bundle is on disk [then ⛔️] nothing recorded for it
    [if] the job succeeded [then ⛔️] nothing recorded
    [if] the job is a scope job [then] the tracks its worker reported failing are recorded
    [if] a scope track is pending but the worker never named it [then ⛔️] nothing recorded for it
    [if] a scope track the worker named is ready or has no audio [then ⛔️] nothing recorded for it
"""

from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from apps.shared.state import locations as state_locations
from apps.shared.state.db import connect_ro
from apps.stems import job as stems_job
from apps.shared.platform_paths import DATA_DIR
from apps.webui.server import coverage_outcomes as outcomes_mod

STEP: str = "stems"
#: The job's error blob carries stdout and stderr tails; the ledger keeps a line.
REASON_MAX_CHARS: int = 300


def _audio_paths(data_dir: Path, stable_ids: list[str]) -> dict[str, Path | None]:
    """Each track's audio on this machine, resolved as the coverage reading does."""
    with connect_ro(data_dir / "state" / "state.db") as conn:
        return state_locations.bulk_local_audio_paths(conn, stable_ids)


#: One entry of a stems worker's ``failures:`` report, as the runner's error
#: tail carries it: ``"  <stable_id>: <error>"``. Both workers
#: (``scripts/stems_modal_worker.py``, ``scripts/stems_local_worker.py``) print
#: that shape. A line that is not one (a header, the session-budget line, an
#: error mentioning a colon) either fails this match or names no library row.
_FAILURE_LINE = re.compile(r"^\s*([^\s:|]+): ")


def _library_ids(data_dir: Path) -> set[str]:
    with connect_ro(data_dir / "state" / "state.db") as conn:
        return {row[0] for row in conn.execute("SELECT stable_id FROM tracks WHERE deleted_at IS NULL")}


def reported_failures(job: dict[str, Any], library_ids: set[str]) -> list[str]:
    """The tracks the worker itself reported failing, in report order, once each.

    Read from the job's error blob, whose stderr tail joins the worker's lines
    with `` | ``. Only an id that is a live library row counts, so a line that
    merely looks like ``word: text`` never becomes a track.
    """
    error = job.get("error")
    if not isinstance(error, str):
        return []
    _, marker, tail = error.rpartition("stderr tail: ")
    if not marker:
        return []
    named: list[str] = []
    for line in tail.split(" | "):
        match = _FAILURE_LINE.match(line)
        if match and match.group(1) in library_ids and match.group(1) not in named:
            named.append(match.group(1))
    return named


def unbundled_targets(job: dict[str, Any]) -> tuple[Path, list[str]] | None:
    """The data dir and the requested tracks a finished job left with no bundle.

    What a failed run actually failed AT: tracks that landed before the worker
    died have their bundle and are not failures, whatever the job row says.
    Presence, as in ``stems_job.reconcile_from_disk``, for the same reason.

    Reads the stored row, which ``POST /api/v1/jobs`` already validated and
    canonicalized (``stems_job.canonical_payload``), and never the tier or
    executor. A scope job (``scope: pending``) resolved its targets inside
    the worker and nothing recorded them, so it is attributed only to the
    tracks the worker's own failure report names (:func:`reported_failures`);
    re-resolving "pending" now would claim tracks the run never attempted.
    None when the row does not have the canonical shape, or the library
    cannot be read: nothing is guessed. Kept out of ``apps.stems`` on
    purpose: it reads the pipeline's output and never changes how a separation runs.
    """
    payload = job.get("payload") or {}
    raw_dir = payload.get("data_dir")
    if raw_dir is not None:
        if not isinstance(raw_dir, str):
            return None
        data_dir = Path(raw_dir)
    else:
        data_dir = DATA_DIR
    measure_roots = stems_job.stems_measure_roots(data_dir)
    raw_ids = payload.get("stable_ids")
    if raw_ids is None and payload.get("scope") == stems_job.SCOPE_PENDING:
        try:
            named = reported_failures(job, _library_ids(data_dir))
        except (FileNotFoundError, sqlite3.Error):
            return None
        return data_dir, stems_job.count_bundles(named, measure_roots).missing
    if not isinstance(raw_ids, list) or not raw_ids:
        return None
    if not all(isinstance(entry, str) and entry for entry in raw_ids):
        return None
    return data_dir, stems_job.count_bundles([str(sid) for sid in raw_ids], measure_roots).missing


def _reason(job: dict[str, Any]) -> str:
    error = str(job.get("error") or "no error recorded")
    head = error.splitlines()[0] if error.strip() else "no error recorded"
    return f"stems job {job['id']} failed: {head}"[:REASON_MAX_CHARS]


def record_stems_job_failure(job: dict[str, Any], *, clock: Callable[[], float] = time.time) -> list[str]:
    """Record a failed stems job's unbundled targets. Returns the ids recorded."""
    if job.get("status") != "failed":
        return []
    targets = unbundled_targets(job)
    if targets is None:
        return []
    data_dir, stable_ids = targets
    if not stable_ids:
        return []
    paths = _audio_paths(data_dir, stable_ids)
    signed = [
        (stable_id, outcomes_mod.audio_token(path))
        for stable_id in stable_ids
        if (path := paths.get(stable_id)) is not None
    ]
    store = outcomes_mod.OutcomeStore(outcomes_mod.store_path(data_dir))
    store.record_failures(STEP, signed, _reason(job), now=clock(), terminal=True)
    return [stable_id for stable_id, _signature in signed]


__all__ = ["REASON_MAX_CHARS", "STEP", "record_stems_job_failure", "reported_failures", "unbundled_targets"]
