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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

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


class LibraryAvailabilityWorker:
    """Background availability probe owned by the engine."""

    def __init__(
        self,
        data_dir: Path,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        state_db_path: Path | None = None,
        on_batch_committed: Callable[[AvailabilityWorkerStatus], None] | None = None,
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
        self._on_batch_committed = on_batch_committed

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
        snapshot.pending = self._count_pending()
        # Recomputed fresh from the database on every call, exactly like
        # `pending` above: `self._status.present` is only ever written inside
        # `_refresh_status_counts`, which a round that finds nothing pending
        # (every row already settled, e.g. a later engine boot) never calls,
        # so relying on the cached field left `present` stuck at the
        # dataclass default of 0 even though the database held present rows.
        snapshot.present = self._count_present()
        snapshot.complete = snapshot.pending == 0 and snapshot.phase not in {
            "queued",
            "running",
        }
        return snapshot

    def _open_conn(self) -> sqlite3.Connection:
        return state_db.open_rw(self._state_db_path)

    def _count_present(self) -> int:
        if not self._state_db_path.is_file():
            return 0
        conn = self._open_conn()
        try:
            return present_count(conn)
        finally:
            conn.close()

    def _count_pending(self) -> int:
        if not self._state_db_path.is_file():
            return 0
        conn = self._open_conn()
        try:
            return self._pending_breakdown(conn)["total"]
        finally:
            conn.close()

    def _pending_breakdown(self, conn: sqlite3.Connection) -> dict[str, int]:
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
              SUM(CASE WHEN a.stable_id IS NULL
                        OR t.updated_at > a.checked_at THEN 1 ELSE 0 END)
            FROM tracks t
            LEFT JOIN track_availability a ON a.stable_id = t.stable_id
            WHERE t.deleted_at IS NULL
            """
        ).fetchone()
        unknown, stale, awaiting, incomplete = row or (0, 0, 0, 0)
        unknown_i = int(unknown or 0)
        stale_i = int(stale or 0)
        awaiting_i = int(awaiting or 0)
        return {
            "unknown": unknown_i,
            "stale": stale_i,
            "awaiting_volume": awaiting_i,
            "total": unknown_i + stale_i + awaiting_i,
        }

    def _refresh_status_counts(self, conn: sqlite3.Connection) -> None:
        breakdown = self._pending_breakdown(conn)
        with self._lock:
            self._status.unknown = breakdown["unknown"]
            self._status.stale = breakdown["stale"]
            self._status.awaiting_volume = breakdown["awaiting_volume"]
            self._status.pending = breakdown["total"]
            self._status.present = present_count(conn)
            if breakdown["total"] == 0 and self._status.phase == "running":
                self._status.phase = "complete"
            self._status.complete = (
                breakdown["total"] == 0
                and self._status.phase not in {"queued", "running"}
            )

    def _collect_round_stable_ids(self, conn: sqlite3.Connection) -> list[str]:
        """Every stable_id this round will touch, gathered before any write.

        Mirrors ``_next_batch``'s source order (the priority queue, then the
        incomplete scan, then the awaiting-volume scan) but drains each
        source to exhaustion in one pass instead of one page at a time, so
        the round's mass-missing decision (LIBM-41) can be made ONCE, before
        any row of the round is probed for writing -- not batch by batch
        after earlier batches have already committed.
        """
        with self._lock:
            priority = list(self._priority_ids)
            self._priority_ids.clear()
            cursor = self._keyset_cursor
            scan_awaiting = self._scan_awaiting_volume

        ids: list[str] = list(priority)
        seen = set(ids)

        if not scan_awaiting:
            rows = conn.execute(_INCOMPLETE_SQL, (cursor, -1)).fetchall()
            for (stable_id,) in rows:
                if stable_id not in seen:
                    ids.append(stable_id)
                    seen.add(stable_id)
            if not rows:
                scan_awaiting = True

        if scan_awaiting:
            rows = conn.execute(_AWAITING_VOLUME_SQL, ("", -1)).fetchall()
            for (stable_id,) in rows:
                if stable_id not in seen:
                    ids.append(stable_id)
                    seen.add(stable_id)

        with self._lock:
            self._scan_awaiting_volume = scan_awaiting
            # A full pass through each source was just taken to exhaustion,
            # so the next round starts a fresh scan rather than resuming a
            # partial page.
            self._keyset_cursor = ""
        return ids

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
        report = avail.write(
            conn,
            rows,
            apply_mass_missing_guard=False,
            allow_mass_missing=False,
            always_refresh_checked_at=True,
        )
        conn.commit()
        stable_ids = [row.stable_id for row in rows]
        with self._lock:
            self._status.processed_total += report.total
            if stable_ids:
                self._keyset_cursor = max(stable_ids)
        return report.changed

    def _run_full_probe(self, conn: sqlite3.Connection, req: _FullProbeRequest) -> None:
        rows = avail.probe(conn)
        avail.write(
            conn,
            rows,
            allow_mass_missing=req.allow_mass_missing,
            apply_mass_missing_guard=True,
            always_refresh_checked_at=True,
        )
        conn.commit()
        with self._lock:
            self._status.processed_total += len(rows)
            self._keyset_cursor = ""
            self._scan_awaiting_volume = False
            self._full_probe = None

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            if not self._wake.wait(timeout=_POLL_IDLE_S):
                if self._count_pending() > 0:
                    with self._lock:
                        phase = self._status.phase
                    if phase not in {"refused", "failed"}:
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
                    self._run_full_probe(conn, full_probe)
                except MassMissingError as exc:
                    conn.rollback()
                    with self._lock:
                        self._status.phase = "refused"
                        self._status.last_error = str(exc)
                        self._full_probe = None
                    return
                self._refresh_status_counts(conn)
                return

            with self._lock:
                self._status.phase = "running"
                self._status.last_error = None

            round_start_present = present_count(conn)
            candidate_ids = self._collect_round_stable_ids(conn)
            batches_committed = 0

            if candidate_ids:
                # LIBM-41 follow-up: decide the whole round's mass-missing
                # question from a dry-run classification of every candidate
                # BEFORE any of them is written, so a refusal can never leave
                # earlier batches' downgrades already committed.
                round_rows = avail.probe_batch(conn, candidate_ids)
                try:
                    guard_round_present_drop(
                        conn,
                        round_rows,
                        round_start_present=round_start_present,
                        allow_mass_missing=False,
                    )
                except MassMissingError as exc:
                    conn.rollback()
                    with self._lock:
                        self._status.phase = "refused"
                        self._status.last_error = str(exc)
                    return

                for start in range(0, len(round_rows), self._batch_size):
                    if self._stop.is_set():
                        break
                    chunk = round_rows[start : start + self._batch_size]
                    self._commit_batch(conn, chunk)
                    batches_committed += 1
                    self._refresh_status_counts(conn)
                    if self._on_batch_committed is not None:
                        self._on_batch_committed(self.status())

            with self._lock:
                if self._priority_ids or self._full_probe is not None:
                    self._status.phase = "queued"
                elif self._status.pending == 0:
                    self._status.phase = "complete"
                    self._scan_awaiting_volume = False
                    self._keyset_cursor = ""
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
    "AvailabilityWorkerStatus",
    "DEFAULT_BATCH_SIZE",
    "LibraryAvailabilityWorker",
    "attach_library_changed_probe",
    "notify_library_changed",
]
