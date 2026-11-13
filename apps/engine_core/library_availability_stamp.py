"""Resolver-stamp methods of the availability worker.

Split from ``apps.engine_core.library_availability`` (#9721).
``_AvailabilityResolverStamp`` is a base class of
``LibraryAvailabilityWorker``; its attributes are set by that class's
``__init__``.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime

from apps.engine_core.library_availability_resolver import (
    _RESOLVER_MARKER_PREFIX,
    _STALE_CHECKED_AT,
    _resolver_marker,
    _stale_availability_except,
    _stored_resolver_fingerprint,
)
from apps.engine_core.library_availability_rounds import _AvailabilityRounds
from apps.shared.state import db as state_db

# Same logger name as before the #9721 split, so log routing and
# Sentry source sites do not move.
log = logging.getLogger("apps.engine_core.library_availability")


class _AvailabilityResolverStamp(_AvailabilityRounds):
    """Path-map stamp checks and resets layered on the capped round."""

    _resolver_invalidate_due: bool
    _observed_resolver_fingerprint: str | None

    def _note_unreadable_path_map(self, exc: Exception) -> None:
        """A path-map read failed during a drain.

        Stay queued. ``failed`` makes later idle polls ignore a repaired
        file. Do not record a fingerprint, so the next successful read
        still looks new. Priority ids a capped round already took are
        requeued by that round's ``except Exception``; this must not
        requeue them again.
        """
        log.exception("availability worker could not read path-map.json during drain")
        with self._lock:
            self._status.phase = "queued"
            self._status.last_error = str(exc)
            self._status.complete = False
            if not self._stop.is_set():
                self._resolver_invalidate_due = True

    def _commit_resolver_marker(
        self,
        conn: sqlite3.Connection,
        current: str,
        *,
        reset_rows: bool,
        preserve_ids: tuple[str, ...] = (),
        retain_sweep_baseline: bool = False,
    ) -> bool:
        """Stamp ``current``. Stale every availability row when ``reset_rows``.

        ``preserve_ids`` are rows this sweep already wrote under ``current``.
        Their ``checked_at`` stays the probe timestamp. A restart that
        matches this stamp must not select them as pending again. The stale
        update is chunked at ``ID_BIND_BATCH`` including its two timestamps.

        When that update stales nothing, the keyset cursor the round just
        committed stays. Blanking it made a successful first empty-map round
        look like the scan had not advanced. Staling any other row, or a
        reset with no preserve list, restarts the cursor: a newly stale row
        can sort at or before the tail, and the incomplete scan would skip it.

        ``retain_sweep_baseline`` is for the deferred empty-map stamp only.
        That stamp runs after the first capped round has already captured
        the pre-migration present count. Clearing the baseline here let the
        next round measure a 50% drop against the already-reduced count
        (LIBM-41). A real resolver change still clears it.

        Returns True, not None: the retry helper uses None when it gave up
        or is shutting down. A database lock is raised for that helper, and
        this attempt does not keep the stamp in that case. The observed
        fingerprint is recorded only after the stamp commits.
        """
        rows_staled = 0
        try:
            conn.execute("BEGIN IMMEDIATE")
            if reset_rows:
                if preserve_ids:
                    rows_staled = _stale_availability_except(conn, preserve_ids)
                else:
                    conn.execute(
                        "UPDATE track_availability SET checked_at = ? WHERE checked_at != ?",
                        (_STALE_CHECKED_AT, _STALE_CHECKED_AT),
                    )
            conn.execute(
                "DELETE FROM schema_meta_markers WHERE marker LIKE ?",
                (_RESOLVER_MARKER_PREFIX + "%",),
            )
            conn.execute(
                "INSERT INTO schema_meta_markers(marker, applied_at) VALUES (?, ?)",
                (_resolver_marker(current), datetime.now(UTC).isoformat()),
            )
            conn.commit()
        except (sqlite3.OperationalError, state_db.StateStoreBusyError):
            if conn.in_transaction:
                conn.rollback()
            raise
        self._observed_resolver_fingerprint = current
        if reset_rows:
            with self._lock:
                if not preserve_ids or rows_staled > 0:
                    self._keyset_cursor = ""
                self._scan_awaiting_volume = False
                if not retain_sweep_baseline:
                    self._sweep_baseline_present = None
        return True

    def _finish_deferred_stamp(
        self,
        conn: sqlite3.Connection,
        current: str,
        *,
        preserve_ids: tuple[str, ...] = (),
    ) -> bool:
        """Stamp the first empty-map generation after the round has run.

        An empty map is not a no-op on that first generation.
        ``resolve_library_path`` still maps ``/PIONEER/...`` through
        ``SHARE_ROOT`` with zero entries, so settled rows must be staled or
        a fresh ``absent`` row stays absent forever. Rows this sweep already
        committed keep their probe ``checked_at``. The stamp stays after
        the round so a busy database can requeue priority ids first.

        False means the write did not commit. The due flag stays set so a
        later idle tick retries it. When the reset leaves rows pending, this
        stays queued instead of letting the caller mark the sweep done.
        """
        wrote = self._attempt_with_lock_retries(
            lambda: self._commit_resolver_marker(
                conn,
                current,
                reset_rows=True,
                preserve_ids=preserve_ids,
                retain_sweep_baseline=True,
            )
        )
        if wrote is None:
            if not self._stop.is_set():
                with self._lock:
                    self._resolver_invalidate_due = True
            return False
        self._refresh_status_counts(conn)
        if self._status.pending > 0 and not self._stop.is_set():
            with self._lock:
                self._status.phase = "queued"
                self._status.complete = False
                self._resolver_invalidate_due = True
        return True

    def _invalidate_if_resolver_changed(
        self,
        conn: sqlite3.Connection,
        current: str,
        *,
        map_empty: bool,
    ) -> str:
        """Make persisted rows stale when the path map or resolver generation changes.

        The incremental scan never selects a fresh ``absent`` row. An upgrade
        that starts honoring ``path-map.json``, or an edit to that file,
        would otherwise leave those tracks missing until a manual full probe.
        The marker is written in the same transaction as the checked_at
        reset, and the keyset cursor restarts, so a later process resumes
        the reprobe instead of skipping rows already settled under the new
        stamp. The marker is a row in this database, so replacing state.db
        replaces the stamp with it.

        Returns ``matched`` when the stored stamp already equals ``current``,
        ``reset`` when this call staled rows and committed the stamp, or
        ``defer`` when there is no stamp and the successfully read map has
        no entries. ``defer`` does not write here. The first empty-map
        generation still changes ``/PIONEER`` classification, so
        ``_finish_deferred_stamp`` stales settled rows after this round.
        Writing the stamp here would take the writer lock before priority
        ids are collected, and a busy database then never reaches the
        round's requeue.

        When the stored fingerprint is not None and differs, or the map has
        entries and the stamp does not match, the existing stale-all-rows
        reset still runs in the same transaction as the marker.

        A database lock on ``reset`` is raised for
        ``_attempt_with_lock_retries``. ``current`` and ``map_empty`` were
        read by the caller. A second read here would raise on a truncated
        file after the caller had already decided the wake was runnable.
        """
        stored = _stored_resolver_fingerprint(conn)
        if stored == current:
            self._observed_resolver_fingerprint = current
            return "matched"
        if stored is None and map_empty:
            return "defer"
        self._commit_resolver_marker(conn, current, reset_rows=True)
        return "reset"
