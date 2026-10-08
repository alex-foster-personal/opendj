"""Bounded, resumable engine worker for ``track_availability``.

Stat-only existence checks run off the request path in a daemon thread with
its own SQLite connection. Progress is persisted through the availability
rows themselves; restart re-selects unknown, stale, and ``awaiting_volume``
candidates from the database.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TypeVar

from apps.mik import availability as avail
from apps.shared.platform_paths import SHARE_ROOT, PathMap, load_path_map
from apps.shared.scan_mass_missing import MassMissingError
from apps.shared.state import db as state_db
from apps.shared.state.machine_identity import get_or_create_machine_id
from apps.shared.state.availability_write import (
    AvailabilityRow,
    guard_round_present_drop,
    present_count,
)
from apps.shared.state.locations import ID_BIND_BATCH

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

# Bump when classification changes in a way the incremental scan would not
# notice on its own. The scan only revisits unknown, stale, and
# awaiting_volume rows, so a persisted `absent` row stays absent until this
# generation or the loaded path map changes.
_AVAILABILITY_RESOLVER_GENERATION = 2
# Lives in state.db. A sidecar (state/availability-resolver.json) does not:
# restore workflows replace only state.db and would keep a matching sidecar
# over rows classified by an older resolver.
_RESOLVER_MARKER_PREFIX = "availability-resolver:"
# This is an invalidation marker, not a sync timestamp. The supported
# tracks.updated_at floor is year one (sync_stamp.FLOOR_STAMP), so neither
# 1970 nor that floor itself makes every row stale under strict `>`.
# Empty TEXT is non-NULL and sorts before every supported track timestamp.
_STALE_CHECKED_AT = ""


class _PathMapUnreadable(Exception):
    """path-map.json could not be read during a drain.

    The drain stays queued and retries. This must not escape to ``_run_loop``,
    which would park the worker in ``failed`` and suppress later wakes.
    """


def _load_path_map(data_dir: Path) -> PathMap:
    """Read path-map.json; raise ``_PathMapUnreadable`` on OSError/ValueError."""
    try:
        return load_path_map(data_dir)
    except (OSError, ValueError) as exc:
        raise _PathMapUnreadable(str(exc)) from exc


def _resolver_env_fingerprint() -> dict[str, str]:
    """Resolver inputs that change targets without a path-map edit.

    ``resolve_library_path`` sends ``/PIONEER/...`` through ``SHARE_ROOT``
    and, when ``MDT_LIBRARY_MODE`` is remote, refuses a native ``/Users/...``
    path. A restart that changes the mode, ``MDT_CRATE_ROOT``, or the share
    root this process imported must not keep settled rows.
    """
    return {
        "crate_root": os.environ.get("MDT_CRATE_ROOT", "").strip(),
        "library_mode": os.environ.get("MDT_LIBRARY_MODE", "").strip().lower(),
        "share_root": str(SHARE_ROOT),
    }


def _resolver_fingerprint_from_map(data_dir: Path, path_map: PathMap) -> str:
    """Fingerprint for an already-read map. Includes this machine's id."""
    env_inputs = _resolver_env_fingerprint()
    payload = {
        "crate_root": env_inputs["crate_root"],
        "entries": [{"from": src, "to": dst} for src, dst in path_map.entries],
        "generation": _AVAILABILITY_RESOLVER_GENERATION,
        "library_mode": env_inputs["library_mode"],
        "machine_id": get_or_create_machine_id(data_dir),
        "share_root": env_inputs["share_root"],
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _resolver_fingerprint(data_dir: Path) -> str:
    """Stable id of this machine, the path map, the resolver generation, and
    the mode, crate root, and share root the resolver is using.

    Machine id lives outside state.db. A restored database must not keep
    another machine's present or absent verdicts just because the path map
    matches, including the common case of an empty map. Mode and crate root
    are the same kind of input: they change ``/PIONEER`` and remote
    ``/Users`` targets while the map file stays byte-identical.
    """
    return _resolver_fingerprint_from_map(data_dir, _load_path_map(data_dir))


def _resolver_marker(fingerprint: str) -> str:
    return f"{_RESOLVER_MARKER_PREFIX}{fingerprint}"


def _stored_resolver_fingerprint(conn: sqlite3.Connection) -> str | None:
    """The fingerprint stored in this database, or None when it must be reset.

    Zero or several markers both mean "not bound to this file". A restored
    state.db brings its own marker (or none); a leftover sidecar is ignored.
    """
    present = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        ("schema_meta_markers",),
    ).fetchone()
    if present is None:
        raise RuntimeError(
            "schema_meta_markers is missing; cannot bind the availability "
            "resolver stamp to state.db"
        )
    rows = conn.execute(
        "SELECT marker FROM schema_meta_markers WHERE marker LIKE ?",
        (_RESOLVER_MARKER_PREFIX + "%",),
    ).fetchall()
    if len(rows) != 1:
        return None
    marker = str(rows[0][0])
    if not marker.startswith(_RESOLVER_MARKER_PREFIX):
        return None
    fingerprint = marker[len(_RESOLVER_MARKER_PREFIX) :]
    if not fingerprint:
        return None
    return fingerprint


def _stale_availability_except(
    conn: sqlite3.Connection, preserve_ids: tuple[str, ...]
) -> int:
    """Stale every availability row whose id is not in ``preserve_ids``.

    One ``NOT IN`` of the whole preserve list binds two timestamps plus one
    variable per id. A first empty-map round can preserve ``MAX_ROUND_IDS``
    (2000) ids, and SQLite builds capped at 999 variables raise "too many
    SQL variables". Chunking that ``NOT IN`` is not the same statement: an
    earlier chunk would stale ids that belong in a later chunk. Select the
    complement, then update it in ``IN`` chunks of ``ID_BIND_BATCH - 2`` so
    each statement binds the two timestamps plus that chunk and nothing more.
    """
    chunk_len = ID_BIND_BATCH - 2
    if chunk_len < 1:
        raise RuntimeError(
            f"ID_BIND_BATCH ({ID_BIND_BATCH}) cannot fit two timestamps "
            "plus one stable_id"
        )
    preserve = set(preserve_ids)
    to_stale = [
        stable_id
        for (stable_id,) in conn.execute(
            "SELECT stable_id FROM track_availability "
            "WHERE checked_at != ? ORDER BY stable_id",
            (_STALE_CHECKED_AT,),
        )
        if stable_id not in preserve
    ]
    changed = 0
    for start in range(0, len(to_stale), chunk_len):
        chunk = to_stale[start : start + chunk_len]
        placeholders = ",".join("?" * len(chunk))
        before = conn.total_changes
        conn.execute(
            "UPDATE track_availability SET checked_at = ? "
            f"WHERE checked_at != ? AND stable_id IN ({placeholders})",
            (_STALE_CHECKED_AT, _STALE_CHECKED_AT, *chunk),
        )
        changed += conn.total_changes - before
    return changed


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
        rows = avail.probe(
            conn, volumes_root=self._volumes_root, path_map=path_map
        )
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

    def _note_unreadable_path_map(self, exc: Exception) -> None:
        """A path-map read failed during a drain.

        Stay queued. ``failed`` makes later idle polls ignore a repaired
        file. Do not record a fingerprint, so the next successful read
        still looks new. Priority ids a capped round already took are
        requeued by that round's ``except Exception``; this must not
        requeue them again.
        """
        log.exception(
            "availability worker could not read path-map.json during drain"
        )
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
                        "UPDATE track_availability SET checked_at = ? "
                        "WHERE checked_at != ?",
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
