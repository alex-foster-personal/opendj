"""Bounded, resumable engine worker for ``track_availability``.

Stat-only existence checks run off the request path in a daemon thread with
its own SQLite connection. Progress is persisted through the availability
rows themselves; restart re-selects unknown, stale, and ``awaiting_volume``
candidates from the database.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypeVar

from apps.mik import availability as avail
from apps.shared.scan_mass_missing import MassMissingError
from apps.shared.state import db as state_db
from apps.shared.state.availability_write import (
    AvailabilityRow,
    guard_round_present_drop,
    present_count,
)

log = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE: int = 100
_POLL_IDLE_S: float = 0.25
AVAILABILITY_LOCK_RETRIES: int = 3
_AVAILABILITY_LOCK_BACKOFF_BASE_S: float = 0.25
_AVAILABILITY_LOCK_BACKOFF_MAX_S: float = 2.0

_T = TypeVar("_T")

#: Upper bound on how many stable_ids a single round gathers before its
#: first write commits. Well above `ID_BIND_BATCH` (500, the SQL bind-count
#: chunk size) so a round rarely needs more than a couple of internal
#: `probe_batch` chunks, but finite so one round's memory and the
#: classification work ahead of its first write batch stay bounded even on
#: a huge library or a slow mounted filesystem -- a shutdown mid-round only
#: waits through one bounded round, not an unbounded scan. A library bigger
#: than this is settled across several rounds: `_collect_round_stable_ids`
#: stops at the cap and leaves the rest for the worker's next wake.
MAX_ROUND_IDS: int = 2000

Phase = Literal["idle", "queued", "running", "complete", "refused", "failed"]

_LIBRARY_PROBE_KINDS = frozenset({"tracks", "reconcile", "ingest"})

_INCOMPLETE_SQL = """
SELECT t.stable_id
FROM tracks t
LEFT JOIN track_availability a ON a.stable_id = t.stable_id
WHERE t.deleted_at IS NULL
  AND (
    a.stable_id IS NULL
    OR t.updated_at > a.checked_at
  )
  AND t.stable_id > ?
ORDER BY t.stable_id
LIMIT ?
"""

_AWAITING_VOLUME_SQL = """
SELECT t.stable_id
FROM tracks t
JOIN track_availability a ON a.stable_id = t.stable_id
WHERE t.deleted_at IS NULL
  AND a.state = 'awaiting_volume'
  AND t.stable_id > ?
ORDER BY t.stable_id
LIMIT ?
"""


@dataclass
class AvailabilityWorkerStatus:
    phase: Phase = "idle"
    pending: int = 0
    unknown: int = 0
    stale: int = 0
    awaiting_volume: int = 0
    processed_total: int = 0
    present: int = 0
    last_error: str | None = None
    complete: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "phase": self.phase,
            "pending": self.pending,
            "unknown": self.unknown,
            "stale": self.stale,
            "awaiting_volume": self.awaiting_volume,
            "processed_total": self.processed_total,
            "present": self.present,
            "last_error": self.last_error,
            "complete": self.complete,
        }


@dataclass
class _FullProbeRequest:
    allow_mass_missing: bool = False


@dataclass
class _RoundCursor:
    """A round's candidate resumption state, not yet committed.

    See ``_collect_round_stable_ids`` / ``_commit_round_cursor``.
    """

    cursor: str
    scan_awaiting: bool
    priority_taken: list[str]


class LibraryAvailabilityWorker:
    """Background availability probe owned by the engine."""

    def __init__(
        self,
        data_dir: Path,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        state_db_path: Path | None = None,
        on_batch_committed: Callable[[AvailabilityWorkerStatus], None] | None = None,
        volumes_root: str | None = None,
    ) -> None:
        self._data_dir = data_dir
        self._state_db_path = state_db_path or (data_dir / "state" / "state.db")
        self._batch_size = batch_size
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._priority_ids: deque[str] = deque()
        self._full_probe: _FullProbeRequest | None = None
        self._status = AvailabilityWorkerStatus()
        self._keyset_cursor = ""
        self._scan_awaiting_volume = False
        #: The ORIGINAL present count when the current sweep began (the
        #: first capped round after the worker last had nothing pending),
        #: not recomputed every round -- see `_drain_once`'s LIBM-41
        #: follow-up note (round-6 Sol finding, cumulative loss guard).
        self._sweep_baseline_present: int | None = None
        self._on_batch_committed = on_batch_committed
        #: Defaults to the real production ``/Volumes`` contract
        #: (`apps.mik.availability.VOLUMES_ROOT`). A caller that needs a
        #: fully isolated volume-root tree for a test passes an explicit
        #: override here (round-6 Sol finding: a test must not monkeypatch
        #: the module constant, a mocked production input).
        self._volumes_root = volumes_root if volumes_root is not None else avail.VOLUMES_ROOT

    def set_on_batch_committed(
        self, callback: Callable[[AvailabilityWorkerStatus], None] | None
    ) -> None:
        """Replace the post-batch-commit callback.

        A test/observability seam: the constructor kwarg cannot be reached once
        ``create_app`` already owns the instance (it is exposed read-only via
        ``app.state.availability_worker``), so this lets a caller install a hook
        on the real worker before calling ``start()``.
        """
        self._on_batch_committed = callback

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="library-availability",
            daemon=True,
        )
        self._thread.start()
        self.request_probe()

    def stop_after_current_batch(self) -> None:
        self._stop.set()
        self._wake.set()

    def stop(self) -> None:
        self.stop_after_current_batch()
        if self._thread is not None:
            self._thread.join(timeout=30.0)
            self._thread = None

    def request_probe(
        self,
        stable_ids: list[str] | None = None,
        *,
        full: bool = False,
        allow_mass_missing: bool = False,
    ) -> None:
        with self._lock:
            if stable_ids:
                self._priority_ids.extend(stable_ids)
            if full:
                self._full_probe = _FullProbeRequest(
                    allow_mass_missing=allow_mass_missing
                )
            if self._status.phase in {"idle", "complete", "refused", "failed"}:
                self._status.phase = "queued"
                self._status.complete = False
        self._wake.set()

    def status(self) -> AvailabilityWorkerStatus:
        with self._lock:
            snapshot = AvailabilityWorkerStatus(**self._status.__dict__)
        # Every counter below comes from ONE query against ONE connection
        # (`_read_status_counts`), so a write landing between two of them
        # cannot make the response internally inconsistent -- e.g. `pending`
        # freshly reflecting a row's new state while `unknown`/`stale`/
        # `awaiting_volume` are still whatever `_refresh_status_counts` last
        # cached (which is what a fresh install, or any status() call before
        # the first round's first batch commits, used to show: pending > 0
        # with unknown/stale/awaiting_volume stuck at 0).
        counts = self._read_status_counts()
        snapshot.unknown = counts["unknown"]
        snapshot.stale = counts["stale"]
        snapshot.awaiting_volume = counts["awaiting_volume"]
        snapshot.pending = counts["pending"]
        snapshot.present = counts["present"]
        snapshot.complete = snapshot.pending == 0 and snapshot.phase not in {
            "queued",
            "running",
        }
        return snapshot

    def _open_conn(self) -> sqlite3.Connection:
        return state_db.open_rw(self._state_db_path)

    def _count_pending(self) -> int:
        if not self._state_db_path.is_file():
            return 0
        conn = self._open_conn()
        try:
            return self._status_counts_from_conn(conn)["pending"]
        finally:
            conn.close()

    def _read_status_counts(self) -> dict[str, int]:
        if not self._state_db_path.is_file():
            return {
                "unknown": 0,
                "stale": 0,
                "awaiting_volume": 0,
                "pending": 0,
                "present": 0,
            }
        conn = self._open_conn()
        try:
            return self._status_counts_from_conn(conn)
        finally:
            conn.close()

    def _status_counts_from_conn(self, conn: sqlite3.Connection) -> dict[str, int]:
        """unknown/stale/awaiting_volume/pending/present, ONE snapshot.

        A single SQL statement (subqueries for the two figures that need a
        different FROM/JOIN shape), so SQLite evaluates it as one atomic
        read even on a WAL-mode, autocommit connection -- unlike splitting
        this into separate queries (or separate connections, the round-4
        Sol finding), a concurrent writer's commit cannot land strictly
        between two of these fields and leave them describing different
        moments.
        """
        row = conn.execute(
            """
            SELECT
              SUM(CASE WHEN a.stable_id IS NULL THEN 1 ELSE 0 END),
              SUM(CASE WHEN a.stable_id IS NOT NULL
                        AND t.updated_at > a.checked_at THEN 1 ELSE 0 END),
              (
                SELECT COUNT(*)
                FROM tracks t2
                JOIN track_availability a2 ON a2.stable_id = t2.stable_id
                WHERE t2.deleted_at IS NULL AND a2.state = 'awaiting_volume'
              ),
              (
                SELECT COUNT(*)
                FROM tracks t3
                JOIN track_availability a3 ON a3.stable_id = t3.stable_id
                WHERE t3.deleted_at IS NULL AND a3.state = 'present'
              ),
              -- pending: the UNION of candidate rows (never-probed, or
              -- needing re-probe), not a sum of the categories above -- a
              -- row can be BOTH stale (t.updated_at > a.checked_at) AND
              -- awaiting_volume (a.state = 'awaiting_volume') at once, and
              -- summing double-counts it (round-5 Sol finding).
              SUM(CASE
                    WHEN a.stable_id IS NULL THEN 1
                    WHEN t.updated_at > a.checked_at THEN 1
                    WHEN a.state = 'awaiting_volume' THEN 1
                    ELSE 0
                  END)
            FROM tracks t
            LEFT JOIN track_availability a ON a.stable_id = t.stable_id
            WHERE t.deleted_at IS NULL
            """
        ).fetchone()
        unknown, stale, awaiting, present, pending = row or (0, 0, 0, 0, 0)
        return {
            "unknown": int(unknown or 0),
            "stale": int(stale or 0),
            "awaiting_volume": int(awaiting or 0),
            "pending": int(pending or 0),
            "present": int(present or 0),
        }

    def _refresh_status_counts(self, conn: sqlite3.Connection) -> None:
        counts = self._status_counts_from_conn(conn)
        with self._lock:
            self._status.unknown = counts["unknown"]
            self._status.stale = counts["stale"]
            self._status.awaiting_volume = counts["awaiting_volume"]
            self._status.pending = counts["pending"]
            self._status.present = counts["present"]
            if counts["pending"] == 0 and self._status.phase == "running":
                self._status.phase = "complete"
            self._status.complete = (
                counts["pending"] == 0
                and self._status.phase not in {"queued", "running"}
            )

    def _collect_round_stable_ids(
        self, conn: sqlite3.Connection
    ) -> tuple[list[str], _RoundCursor]:
        """Every stable_id THIS round will touch, capped at MAX_ROUND_IDS.

        Mirrors ``_next_batch``'s source order (the priority queue, then the
        incomplete scan, then the awaiting-volume scan), draining each
        source as far as the cap allows in one pass instead of one
        LIMIT-bounded page at a time, so the round's mass-missing decision
        (LIBM-41) can still be made ONCE over the whole (bounded) round,
        before any row of it is probed for writing -- not batch by batch
        after earlier batches have already committed (round 2), and not by
        stat-ing an entire unbounded library before the first write commits
        (round 4).

        The candidate next cursor/scan-source and the priority ids taken
        for this round are returned as a :class:`_RoundCursor`, NOT written
        to ``self._keyset_cursor`` / ``self._scan_awaiting_volume`` /
        ``self._priority_ids`` here -- ``_drain_once`` commits that only
        after this round's guard has passed and its writes have committed
        (``_commit_round_cursor``), or restores the priority ids
        (``_requeue_priority_ids``) if the round is refused or raises
        (round-6 Sol finding: advancing the cursor before a guarded write
        succeeds could resume PAST rows that were never actually written,
        skipping them forever once the scan later runs off the end of the
        table).
        """
        with self._lock:
            take_n = min(len(self._priority_ids), MAX_ROUND_IDS)
            priority = [self._priority_ids.popleft() for _ in range(take_n)]
            cursor = self._keyset_cursor
            scan_awaiting = self._scan_awaiting_volume

        ids: list[str] = list(priority)
        seen = set(ids)

        if not scan_awaiting:
            remaining = MAX_ROUND_IDS - len(ids)
            if remaining > 0:
                rows = conn.execute(_INCOMPLETE_SQL, (cursor, remaining)).fetchall()
                for (stable_id,) in rows:
                    if stable_id not in seen:
                        ids.append(stable_id)
                        seen.add(stable_id)
                if rows:
                    cursor = rows[-1][0]
                if len(rows) < remaining:
                    # Incomplete scan exhausted at this cursor: the next
                    # source picks up any capacity left in THIS round.
                    scan_awaiting = True
                    cursor = ""

        if scan_awaiting:
            remaining = MAX_ROUND_IDS - len(ids)
            if remaining > 0:
                rows = conn.execute(_AWAITING_VOLUME_SQL, (cursor, remaining)).fetchall()
                for (stable_id,) in rows:
                    if stable_id not in seen:
                        ids.append(stable_id)
                        seen.add(stable_id)
                cursor = rows[-1][0] if rows else ""

        return ids, _RoundCursor(
            cursor=cursor, scan_awaiting=scan_awaiting, priority_taken=priority
        )

    def _commit_round_cursor(self, round_cursor: _RoundCursor) -> None:
        """Advance the resumption state -- ONLY after a round's guard has
        passed and its writes have committed (see `_collect_round_stable_ids`).
        """
        with self._lock:
            self._keyset_cursor = round_cursor.cursor
            self._scan_awaiting_volume = round_cursor.scan_awaiting

    def _attempt_with_lock_retries(self, fn: Callable[[], _T]) -> _T | None:
        """Run ``fn``; retry transient SQLITE_BUSY up to :data:`AVAILABILITY_LOCK_RETRIES`."""
        for attempt in range(AVAILABILITY_LOCK_RETRIES):
            try:
                return fn()
            except (sqlite3.OperationalError, state_db.StateStoreBusyError) as exc:
                # writer_common wraps SQLITE_BUSY as StateStoreBusyError since
                # 0a0d095fb; both shapes are the same transient lock.
                if isinstance(exc, sqlite3.OperationalError) and not state_db.is_sqlite_busy(exc):
                    raise
                if attempt + 1 >= AVAILABILITY_LOCK_RETRIES:
                    # Do not park in "failed". That phase suppresses the
                    # idle wake, so unchecked rows sit forever until someone
                    # hands their ids to request_probe. Stay queued, say so
                    # at error level, and let the loop drain again.
                    log.error(
                        "availability worker gave up this round after %s "
                        "database lock retries; unchecked rows stay queued",
                        AVAILABILITY_LOCK_RETRIES,
                    )
                    with self._lock:
                        self._status.phase = "queued"
                        self._status.last_error = (
                            f"availability writes blocked after "
                            f"{AVAILABILITY_LOCK_RETRIES} lock retries"
                        )
                        self._status.complete = False
                    return None
                log.warning(
                    "availability worker database locked, retry %s/%s",
                    attempt + 1,
                    AVAILABILITY_LOCK_RETRIES,
                )
                with self._lock:
                    self._status.phase = "queued"
                    self._status.last_error = str(exc)
                backoff = min(
                    _AVAILABILITY_LOCK_BACKOFF_BASE_S * (2 ** attempt),
                    _AVAILABILITY_LOCK_BACKOFF_MAX_S,
                )
                if self._stop.wait(timeout=backoff):
                    return None
        return None

    def _requeue_priority_ids(self, stable_ids: list[str]) -> None:
        """Restore priority ids a refused/failed round took but never wrote.

        Put back at the FRONT of the queue, in their original order, so a
        retry re-sees them ahead of anything requested later.
        """
        if not stable_ids:
            return
        with self._lock:
            self._priority_ids.extendleft(reversed(stable_ids))

    def _run_capped_round(self, conn: sqlite3.Connection) -> tuple[int, bool]:
        """Collect, guard, and write one capped round. Returns
        ``(batches_committed, refused)``.

        Refusal sets ``phase``/``last_error`` itself (the caller only needs
        to know to stop); every other path leaves phase-setting to the
        caller's own tail logic.
        """
        candidate_ids, round_cursor = self._collect_round_stable_ids(conn)
        if not candidate_ids:
            # Nothing to probe or write this round (e.g. the incomplete
            # scan just exhausted with capacity left over): no guard or
            # write was attempted that could still fail, so the source
            # flip / cursor reset is safe to commit immediately.
            self._commit_round_cursor(round_cursor)
            return 0, False

        # The mass-missing baseline for the whole SWEEP (not just this
        # round): captured once, the first time a round in this sweep has
        # candidates, and reused on every later round of the same sweep. A
        # library bigger than the round cap needs several rounds to
        # settle; recomputing the baseline from the ALREADY-REDUCED
        # present count every round (round-4/5 behavior) let N capped
        # rounds each stay individually under the 50% LIBM-41 threshold
        # while cumulatively wiping out far more of the library than that
        # (round-6 Sol finding, BLOCKING P1). Reset to None when a sweep
        # completes (`_drain_once`'s tail) or a full probe resets the
        # world (`_run_full_probe`).
        if self._sweep_baseline_present is None:
            with self._lock:
                self._sweep_baseline_present = present_count(conn)
        round_start_present = self._sweep_baseline_present

        # LIBM-41 follow-up: decide the whole round's mass-missing question
        # from a dry-run classification of every candidate BEFORE any of
        # them is written, so a refusal can never leave earlier batches'
        # downgrades already committed.
        try:
            round_rows = avail.probe_batch(
                conn, candidate_ids, volumes_root=self._volumes_root
            )
            guard_round_present_drop(
                conn,
                round_rows,
                round_start_present=round_start_present,
                allow_mass_missing=False,
            )
        except MassMissingError as exc:
            conn.rollback()
            # The round never wrote anything: the priority ids it took
            # must go back so a retry re-sees them, and the cursor/scan-
            # source must stay exactly where `_collect_round_stable_ids`
            # found them (round-6 Sol finding) -- simply never calling
            # `_commit_round_cursor` achieves that.
            self._requeue_priority_ids(round_cursor.priority_taken)
            with self._lock:
                self._status.phase = "refused"
                self._status.last_error = str(exc)
            return 0, True
        except Exception:
            conn.rollback()
            self._requeue_priority_ids(round_cursor.priority_taken)
            raise

        batches_committed = 0
        round_completed = True
        for start in range(0, len(round_rows), self._batch_size):
            if self._stop.is_set():
                round_completed = False
                break
            chunk = round_rows[start : start + self._batch_size]
            try:
                self._commit_batch(conn, chunk)
            except (sqlite3.OperationalError, state_db.StateStoreBusyError) as exc:
                if isinstance(exc, state_db.StateStoreBusyError) or state_db.is_sqlite_busy(exc):
                    # The whole round, not only the priority subset. The
                    # cursor is not advanced, and the batch must be offered
                    # again ahead of later scans.
                    self._requeue_priority_ids(list(candidate_ids))
                    raise
                raise
            batches_committed += 1
            self._refresh_status_counts(conn)
            if self._on_batch_committed is not None:
                self._on_batch_committed(self.status())

        if round_completed:
            # Guard passed AND every row of the round is durably written:
            # only now may the resumption state advance.
            self._commit_round_cursor(round_cursor)
        else:
            # Stopped mid-round: rows already committed are safe
            # (idempotent, already have a fresh track_availability row so
            # a re-scan naturally skips them), but the cursor must not
            # jump to the round's full tail, or the rows past the last
            # committed chunk would be silently skipped next time. Any
            # priority id may already have been written in an earlier
            # chunk this round; requeueing it anyway is a redundant,
            # idempotent re-probe next round, never a skip.
            self._requeue_priority_ids(round_cursor.priority_taken)

        return batches_committed, False

    def _commit_batch(
        self,
        conn: sqlite3.Connection,
        rows: list[AvailabilityRow],
    ) -> int:
        """Durably write a pre-probed, already guard-decided slice of the round.

        The mass-missing decision for the whole round is made once in
        ``_drain_once``, before any row here was even probed for writing, so
        this only chunks the writes and status refreshes -- it must not
        re-run the guard against a partial slice.
        """
        report = avail.commit_availability_batch(
            conn,
            rows,
            apply_mass_missing_guard=False,
            allow_mass_missing=False,
            always_refresh_checked_at=True,
            existing_scope_stable_ids=[row.stable_id for row in rows],
        )
        # Do NOT derive self._keyset_cursor from `rows` here: `rows` is a
        # chunk of `round_rows`, which mixes priority-queue ids in ahead of
        # the keyset-scanned ones (`_collect_round_stable_ids`'s own
        # ordering). A priority id supplied via library.changed sorts
        # arbitrarily relative to the scan's true position, and taking
        # max(stable_ids) over a chunk that contains one can advance the
        # cursor PAST rows the keyset scan itself never visited, so a later
        # round's `WHERE stable_id > cursor` silently skips them forever
        # (round-5 Sol finding, BLOCKING P1). `_collect_round_stable_ids`
        # already set the correct cursor for this round, from the keyset
        # scan's own last-returned row only, before any of these rows were
        # even probed -- this method must leave that value alone.
        with self._lock:
            self._status.processed_total += report.total
        return report.changed

    def _run_full_probe(self, conn: sqlite3.Connection, req: _FullProbeRequest) -> bool:
        rows = avail.probe(conn, volumes_root=self._volumes_root)
        avail.commit_availability_batch(
            conn,
            rows,
            allow_mass_missing=req.allow_mass_missing,
            apply_mass_missing_guard=True,
            always_refresh_checked_at=True,
        )
        with self._lock:
            self._status.processed_total += len(rows)
            self._keyset_cursor = ""
            self._scan_awaiting_volume = False
            self._sweep_baseline_present = None
            self._full_probe = None
        # True, not None: `_attempt_with_lock_retries` uses None for
        # "gave up or shutting down", and a full probe has no other result.
        return True

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            if not self._wake.wait(timeout=_POLL_IDLE_S):
                with self._lock:
                    phase = self._status.phase
                    # A full probe (and any priority ids taken back after a
                    # locked round) is queued work even when every live row
                    # is already settled. Pending-only wake left that request
                    # stuck after the retry budget was spent.
                    queued_request = (
                        self._full_probe is not None or bool(self._priority_ids)
                    )
                if phase not in {"refused", "failed"} and (
                    queued_request or self._count_pending() > 0
                ):
                    self._wake.set()
                continue
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                self._drain_once()
            except Exception as exc:
                log.exception("availability worker round failed")
                with self._lock:
                    self._status.phase = "failed"
                    self._status.last_error = str(exc)
                    self._status.complete = False

    def _drain_once(self) -> None:
        conn = self._open_conn()
        try:
            with self._lock:
                full_probe = self._full_probe
                if full_probe is not None:
                    self._status.phase = "running"
                    self._status.last_error = None
            if full_probe is not None:
                try:
                    ran = self._attempt_with_lock_retries(
                        lambda: self._run_full_probe(conn, full_probe)
                    )
                except MassMissingError as exc:
                    conn.rollback()
                    with self._lock:
                        self._status.phase = "refused"
                        self._status.last_error = str(exc)
                        self._full_probe = None
                    return
                if ran is None:
                    return
                self._refresh_status_counts(conn)
                return

            with self._lock:
                self._status.phase = "running"
                self._status.last_error = None

            round_result = self._attempt_with_lock_retries(
                lambda: self._run_capped_round(conn)
            )
            if round_result is None:
                return
            batches_committed, refused = round_result
            if refused:
                return

            with self._lock:
                if self._priority_ids or self._full_probe is not None:
                    self._status.phase = "queued"
                elif self._status.pending == 0:
                    self._status.phase = "complete"
                    self._scan_awaiting_volume = False
                    self._keyset_cursor = ""
                    self._sweep_baseline_present = None
                elif batches_committed == 0:
                    self._status.phase = "idle"
                else:
                    self._status.phase = "idle"
        finally:
            conn.close()


def notify_library_changed(
    worker: LibraryAvailabilityWorker | None,
    stable_ids: list[str] | None = None,
) -> None:
    """Optional in-process nudge after ingest or relink."""
    if worker is None:
        return
    worker.request_probe(stable_ids)


def attach_library_changed_probe(
    worker: LibraryAvailabilityWorker,
    hub: Any,
) -> Any:
    """Wrap ``hub.publish`` so track mutations also nudge the worker."""
    original_publish = hub.publish

    def publish_with_availability(topic: str, payload: dict[str, Any]) -> None:
        original_publish(topic, payload)
        if topic != "library.changed":
            return
        kind = payload.get("kind")
        if kind not in _LIBRARY_PROBE_KINDS:
            return
        raw_ids = payload.get("ids")
        stable_ids = (
            [str(stable_id) for stable_id in raw_ids]
            if isinstance(raw_ids, list) and raw_ids
            else None
        )
        notify_library_changed(worker, stable_ids)

    hub.publish = publish_with_availability  # type: ignore[method-assign]
    return original_publish


__all__ = [
    "AVAILABILITY_LOCK_RETRIES",
    "DEFAULT_BATCH_SIZE",
    "MAX_ROUND_IDS",
    "AvailabilityWorkerStatus",
    "LibraryAvailabilityWorker",
    "attach_library_changed_probe",
    "notify_library_changed",
]
