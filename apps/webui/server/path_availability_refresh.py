"""Background path availability refresher (issue #1037).

Daemon thread started from the webui lifespan. Dedupes paths, batches stats,
and upserts into ``path_availability`` without blocking request handlers.

It is also the ONE writer of that index while the server runs (STATE-18): a
request that stats paths itself hands the answers over with :func:`record`
instead of opening a write connection. A read endpoint that writes waits on
whoever holds the state.db writer lock, so ``GET /api/v1/tracks`` answered
503 ``STATE_STORE_BUSY`` for every write held past ``busy_timeout`` (the
installed app at boot, Mon 5 Oct 2026). A persist that finds the lock busy
logs it and keeps the rows for the next tick rather than killing the thread.
"""
from __future__ import annotations

import logging
import queue
import sqlite3
import threading
import time
from collections.abc import Iterable, Sequence
from pathlib import Path

from apps.adapters.rekordbox import config
from apps.shared.state import db as state_db

from .boot_grace import NO_GRACE, BootGrace
from .rb_vendor_pkg import path_index

log = logging.getLogger(__name__)

_BATCH_SIZE: int = 64


class PathAvailabilityRefresher:
    def __init__(self, *, data_dir: Path, state_db_path: Path, grace: BootGrace = NO_GRACE) -> None:
        self._data_dir = Path(data_dir)
        self._state_db_path = Path(state_db_path)
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._pending: set[str] = set()
        # Answers waiting to be persisted, keyed by (resolver namespace, path)
        # so a re-probe before the next flush replaces rather than duplicates.
        self._unpersisted: dict[tuple[str, str], int | None] = {}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._grace = grace

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="path-availability-refresh",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._queue.put(None)
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)

    def schedule(self, paths: Iterable[str]) -> None:
        for path in paths:
            if not path:
                continue
            with self._lock:
                if path in self._pending:
                    continue
                self._pending.add(path)
            self._queue.put(path)

    def record(self, namespace: str, rows: Sequence[tuple[str, int | None]]) -> None:
        """Queue answers a request already statted; this thread persists them."""
        with self._lock:
            self._unpersisted.update(((namespace, path), size) for path, size in rows)

    def _run(self) -> None:
        # PERF-BOOT-01: the first pass waits for the launch's library index. Paths
        # scheduled meanwhile queue up; answers a request recorded are kept.
        self._grace.wait(self._stop)
        batch: list[str] = []
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                item = "tick"
            if item is None:
                break
            if item != "tick":
                batch.append(item)
            if len(batch) < _BATCH_SIZE and item != "tick" and not self._stop.is_set():
                continue
            if batch:
                self._flush(batch)
                batch.clear()
            self._persist_unpersisted()

        if batch:
            self._flush(batch)
        self._persist_unpersisted()

    def _flush(self, paths: Sequence[str]) -> None:
        now = time.monotonic()
        rows = [(path, path_index.stat_logical_path(path)) for path in paths]
        if not rows:
            return
        with config._FILE_EXISTS_LOCK:
            for path, size in rows:
                config._FILE_EXISTS_CACHE[path] = (now, size)
        self.record(path_index.resolver_namespace(self._data_dir), rows)
        with self._lock:
            for path in paths:
                self._pending.discard(path)

    def _persist_unpersisted(self) -> None:
        """Persist every queued answer; on a busy writer lock, log and keep them.

        The answers are already in the L1 cache, so a deferred persist costs
        only restart warmth, never a request. Kept rows retry on the next tick
        (a newer answer for the same path wins). Anything other than lock
        contention propagates.
        """
        with self._lock:
            taken = self._unpersisted
            self._unpersisted = {}
        if not taken:
            return
        try:
            self._persist(taken)
        except sqlite3.OperationalError as exc:
            if not state_db.is_sqlite_busy(exc):
                raise
            log.warning(
                "path availability index: state.db writer lock still busy after "
                "busy_timeout; %d row(s) kept for the next tick: %s",
                len(taken),
                exc,
            )
            with self._lock:
                for key, size in taken.items():
                    self._unpersisted.setdefault(key, size)

    def _persist(self, rows: dict[tuple[str, str], int | None]) -> None:
        """Upsert into an EXISTING state.db only, in one connection.

        Never creates it: ``open_rw`` on a missing file makes the file first
        and the tables after, so a background create races every request
        that gates on ``STATE_DB.exists()`` into "no such table" (and would
        leave a state layer on a machine that never had one). The answers
        still land in the L1 cache either way.
        """
        if not self._state_db_path.exists():
            return
        by_namespace: dict[str, list[tuple[str, int | None]]] = {}
        for (namespace, path), size in rows.items():
            by_namespace.setdefault(namespace, []).append((path, size))
        conn = state_db.open_rw(self._state_db_path)
        try:
            for namespace, namespace_rows in by_namespace.items():
                path_index.upsert_rows(conn, namespace, namespace_rows)
            conn.commit()
        finally:
            conn.close()


# The process's one refresher, held in a container rather than rebound
# through ``global`` so configure/start/stop share it by reference.
_ACTIVE: dict[str, PathAvailabilityRefresher] = {}


def configure(*, data_dir: Path, state_db_path: Path, grace: BootGrace = NO_GRACE) -> None:
    _ACTIVE["refresher"] = PathAvailabilityRefresher(
        data_dir=data_dir,
        state_db_path=state_db_path,
        grace=grace,
    )


def start() -> None:
    refresher = _ACTIVE.get("refresher")
    if refresher is None:
        raise RuntimeError("path availability refresher is not configured")
    refresher.start()


def start_for_state_db(state_db_path: Path, grace: BootGrace = NO_GRACE) -> None:
    """Configure and start against ``<data_dir>/state/state.db``."""
    db = Path(state_db_path)
    data_dir = db.parent.parent if db.parent.name == "state" else db.parent
    configure(data_dir=data_dir, state_db_path=db, grace=grace)
    start()


def stop() -> None:
    refresher = _ACTIVE.get("refresher")
    if refresher is not None:
        refresher.stop()


def schedule(paths: Iterable[str]) -> None:
    refresher = _ACTIVE.get("refresher")
    if refresher is not None:
        refresher.schedule(paths)


def record(namespace: str, rows: Sequence[tuple[str, int | None]]) -> bool:
    """Hand already-statted answers to the running refresher to persist.

    Returns False when no refresher thread is running (a CLI, a script, or a
    test with no lifespan): that caller has no background writer and must
    persist the rows itself. True means the refresher owns them now.
    """
    refresher = _ACTIVE.get("refresher")
    if refresher is None or not refresher.running:
        return False
    refresher.record(namespace, rows)
    return True


__all__ = ["configure", "record", "schedule", "start", "start_for_state_db", "stop"]
