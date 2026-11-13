"""Worker state types and the capped availability round.

Split from ``apps.engine_core.library_availability`` (#9721), which
re-exports every name here. ``_AvailabilityRounds`` is a base class of
``LibraryAvailabilityWorker``; its attributes are set by that class's
``__init__``.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypeVar

from apps.engine_core.library_availability_resolver import _load_path_map
from apps.mik import availability as avail
from apps.shared.scan_mass_missing import MassMissingError
from apps.shared.state import db as state_db
from apps.shared.state.availability_write import (
    AvailabilityRow,
    guard_round_present_drop,
    present_count,
)

# Same logger name as before the #9721 split, so log routing and
# Sentry source sites do not move.
log = logging.getLogger("apps.engine_core.library_availability")

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


class _AvailabilityRounds:
    """Capped-round probing, lock retries and the full probe for the worker."""

    _data_dir: Path
    _batch_size: int
    _stop: threading.Event
    _lock: threading.Lock
    _priority_ids: deque[str]
    _full_probe: _FullProbeRequest | None
    _status: AvailabilityWorkerStatus
    _keyset_cursor: str
    _scan_awaiting_volume: bool
    _sweep_baseline_present: int | None
    _on_batch_committed: Callable[[AvailabilityWorkerStatus], None] | None
    _volumes_root: str

    if TYPE_CHECKING:

        def status(self) -> AvailabilityWorkerStatus:
            """Defined on ``LibraryAvailabilityWorker``."""
            ...

        def _refresh_status_counts(self, conn: sqlite3.Connection) -> None:
            """Defined on ``LibraryAvailabilityWorker``."""
            ...

    def _collect_round_stable_ids(self, conn: sqlite3.Connection) -> tuple[list[str], _RoundCursor]:
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

        return ids, _RoundCursor(cursor=cursor, scan_awaiting=scan_awaiting, priority_taken=priority)

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
                            f"availability writes blocked after {AVAILABILITY_LOCK_RETRIES} lock retries"
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
                    _AVAILABILITY_LOCK_BACKOFF_BASE_S * (2**attempt),
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

    def _run_capped_round(self, conn: sqlite3.Connection) -> tuple[int, bool, list[str]]:
        """Collect, guard, and write one capped round. Returns
        ``(batches_committed, refused, written_ids)``.

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
            return 0, False, []

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
        # world (`_run_full_probe`). The deferred empty-map stamp stales
        # rows inside this same sweep, so it must not clear the baseline.
        if self._sweep_baseline_present is None:
            with self._lock:
                self._sweep_baseline_present = present_count(conn)
        round_start_present = self._sweep_baseline_present

        # LIBM-41 follow-up: decide the whole round's mass-missing question
        # from a dry-run classification of every candidate BEFORE any of
        # them is written, so a refusal can never leave earlier batches'
        # downgrades already committed.
        try:
            path_map = _load_path_map(self._data_dir)
            round_rows = avail.probe_batch(
                conn,
                candidate_ids,
                volumes_root=self._volumes_root,
                path_map=path_map,
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
            return 0, True, []
        except Exception:
            conn.rollback()
            self._requeue_priority_ids(round_cursor.priority_taken)
            raise

        batches_committed = 0
        round_completed = True
        written_ids: list[str] = []
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
            written_ids.extend(row.stable_id for row in chunk)
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

        return batches_committed, False, written_ids

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
        path_map = _load_path_map(self._data_dir)
        rows = avail.probe(conn, volumes_root=self._volumes_root, path_map=path_map)
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
