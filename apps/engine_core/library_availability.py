"""Bounded, resumable engine worker for ``track_availability``.

Stat-only existence checks run off the request path in a daemon thread with
its own SQLite connection. Progress is persisted through the availability
rows themselves; restart re-selects unknown, stale, and ``awaiting_volume``
candidates from the database.

The resolver fingerprint, the capped round and the resolver stamp live in
the ``library_availability_resolver``, ``library_availability_rounds`` and
``library_availability_stamp`` siblings (#9721); this module re-exports
their names so callers and tests keep importing from here.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from apps.engine_core.library_availability_resolver import (
    _AVAILABILITY_RESOLVER_GENERATION,  # noqa: F401
    _RESOLVER_MARKER_PREFIX,  # noqa: F401
    _STALE_CHECKED_AT,  # noqa: F401
    _load_path_map,
    _PathMapUnreadable,
    _resolver_env_fingerprint,  # noqa: F401
    _resolver_fingerprint,
    _resolver_fingerprint_from_map,
    _resolver_marker,  # noqa: F401
    _stale_availability_except,  # noqa: F401
    _stored_resolver_fingerprint,  # noqa: F401
)
from apps.engine_core.library_availability_rounds import (
    _AVAILABILITY_LOCK_BACKOFF_BASE_S,  # noqa: F401
    _AVAILABILITY_LOCK_BACKOFF_MAX_S,  # noqa: F401
    _AWAITING_VOLUME_SQL,  # noqa: F401
    _INCOMPLETE_SQL,  # noqa: F401
    AVAILABILITY_LOCK_RETRIES,
    MAX_ROUND_IDS,
    AvailabilityWorkerStatus,
    Phase,  # noqa: F401
    _FullProbeRequest,
    _RoundCursor,  # noqa: F401
)
from apps.engine_core.library_availability_stamp import _AvailabilityResolverStamp
from apps.mik import availability as avail
from apps.shared.scan_mass_missing import MassMissingError
from apps.shared.state import db as state_db

log = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE: int = 100
_POLL_IDLE_S: float = 0.25

_LIBRARY_PROBE_KINDS = frozenset({"tracks", "reconcile", "ingest"})


class LibraryAvailabilityWorker(_AvailabilityResolverStamp):
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
        #: A path-map stamp reset that could not commit. No row is pending
        #: until that reset lands, so the idle loop must treat this as
        #: queued work or the sweep never retries it.
        self._resolver_invalidate_due = False
        #: Fingerprint of the path map whose stamp matched or committed.
        #: None until then, so an unseen map is not treated as current.
        #: The idle tick compares a fresh file read with this and does not
        #: open state.db to decide.
        self._observed_resolver_fingerprint: str | None = None
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

    def _idle_poll_should_wake(
        self,
        *,
        phase: str,
        fingerprint_changed: bool,
        queued_request: bool,
    ) -> bool:
        """Decide whether an idle poll should set the wake event.

        A refusing drain already stored the bad fingerprint on
        ``_observed_resolver_fingerprint``. A corrected file compares
        different and must wake; an unchanged file compares equal and
        must stay refused even if priority ids, a full probe, or a leftover
        due flag are set. Failed stays failed even when the fingerprint
        changed. Every other phase wakes on a queued request or pending
        rows.
        """
        if phase == "failed":
            return False
        if phase == "refused":
            return fingerprint_changed
        return queued_request or self._count_pending() > 0

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            if not self._wake.wait(timeout=_POLL_IDLE_S):
                # path-map.json can change after the sweep is complete or
                # after a resolver-induced refusal. Nothing else sets the
                # due flag for that edit, and the file fingerprint is
                # enough: do not open state.db here.
                # A half-written or unreadable map raises before the round's
                # handler. Catch it here so the daemon thread stays alive and
                # the next idle poll can read the repaired file. Do not mark
                # the phase failed: that would refuse the later wake.
                try:
                    current_fingerprint = _resolver_fingerprint(self._data_dir)
                except Exception as exc:
                    log.exception(
                        "availability worker could not read path-map.json while idle"
                    )
                    with self._lock:
                        self._status.last_error = str(exc)
                    continue
                with self._lock:
                    phase = self._status.phase
                    fingerprint_changed = (
                        current_fingerprint != self._observed_resolver_fingerprint
                    )
                    if fingerprint_changed:
                        self._resolver_invalidate_due = True
                    # A full probe, priority ids taken back after a locked
                    # round, a resolver-stamp reset that could not commit,
                    # or a path map this worker has not stamped yet are
                    # queued work even when every live row is already
                    # settled. Pending-only wake left that request stuck
                    # after the retry budget was spent.
                    queued_request = (
                        self._full_probe is not None
                        or bool(self._priority_ids)
                        or self._resolver_invalidate_due
                    )
                if self._idle_poll_should_wake(
                    phase=phase,
                    fingerprint_changed=fingerprint_changed,
                    queued_request=queued_request,
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
        try:
            path_map = _load_path_map(self._data_dir)
            current_fingerprint = _resolver_fingerprint_from_map(
                self._data_dir, path_map
            )
        except _PathMapUnreadable as exc:
            self._note_unreadable_path_map(exc)
            return
        conn = self._open_conn()
        try:
            sync = self._attempt_with_lock_retries(
                lambda: self._invalidate_if_resolver_changed(
                    conn,
                    current_fingerprint,
                    map_empty=not path_map.entries,
                )
            )
            if sync is None:
                # The stamp did not advance, so no row is pending yet. Stay
                # queued (the retry helper already set that phase) and let
                # the idle loop drain again. A failed phase would suppress
                # that wake until an external request_probe.
                if not self._stop.is_set():
                    with self._lock:
                        self._resolver_invalidate_due = True
                return
            with self._lock:
                self._resolver_invalidate_due = False
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
                    # Hold this empty-map attempt. Do not stamp or rewrite
                    # settled rows; the idle poll compares this value.
                    self._observed_resolver_fingerprint = current_fingerprint
                    return
                except _PathMapUnreadable as exc:
                    self._note_unreadable_path_map(exc)
                    return
                if ran is None:
                    return
                if sync == "defer" and not self._finish_deferred_stamp(
                    conn, current_fingerprint
                ):
                    return
                self._refresh_status_counts(conn)
                return

            with self._lock:
                self._status.phase = "running"
                self._status.last_error = None

            try:
                round_result = self._attempt_with_lock_retries(
                    lambda: self._run_capped_round(conn)
                )
            except _PathMapUnreadable as exc:
                self._note_unreadable_path_map(exc)
                return
            if round_result is None:
                return
            batches_committed, refused, written_ids = round_result
            if refused:
                self._observed_resolver_fingerprint = current_fingerprint
                return
            if sync == "defer" and not self._finish_deferred_stamp(
                conn,
                current_fingerprint,
                preserve_ids=tuple(written_ids),
            ):
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
