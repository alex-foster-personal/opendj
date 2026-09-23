"""Background path availability refresher (issue #1037).

Daemon thread started from the webui lifespan. Dedupes paths, batches stats,
and upserts into ``path_availability`` without blocking request handlers.
"""
from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Iterable, Sequence
from pathlib import Path

from apps.shared.state import db as state_db

from .rb_vendor_pkg import path_index

log = logging.getLogger(__name__)

_BATCH_SIZE: int = 64


class PathAvailabilityRefresher:
    def __init__(self, *, data_dir: Path, state_db_path: Path) -> None:
        self._data_dir = Path(data_dir)
        self._state_db_path = Path(state_db_path)
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._pending: set[str] = set()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
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

    def _run(self) -> None:
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

        if batch:
            self._flush(batch)

    def _flush(self, paths: Sequence[str]) -> None:
        namespace = path_index.resolver_namespace(self._data_dir)
        rows = [(path, path_index.stat_logical_path(path)) for path in paths]
        if not rows:
            return
        conn = state_db.open_rw(self._state_db_path)
        try:
            path_index.upsert_rows(conn, namespace, rows)
            conn.commit()
        finally:
            conn.close()
        with self._lock:
            for path in paths:
                self._pending.discard(path)


# The process's one refresher, held in a container rather than rebound
# through ``global`` so configure/start/stop share it by reference.
_ACTIVE: dict[str, PathAvailabilityRefresher] = {}


def configure(*, data_dir: Path, state_db_path: Path) -> None:
    _ACTIVE["refresher"] = PathAvailabilityRefresher(
        data_dir=data_dir,
        state_db_path=state_db_path,
    )


def start() -> None:
    refresher = _ACTIVE.get("refresher")
    if refresher is None:
        raise RuntimeError("path availability refresher is not configured")
    refresher.start()


def start_for_state_db(state_db_path: Path) -> None:
    """Configure and start against ``<data_dir>/state/state.db``."""
    db = Path(state_db_path)
    data_dir = db.parent.parent if db.parent.name == "state" else db.parent
    configure(data_dir=data_dir, state_db_path=db)
    start()


def stop() -> None:
    refresher = _ACTIVE.get("refresher")
    if refresher is not None:
        refresher.stop()


def schedule(paths: Iterable[str]) -> None:
    refresher = _ACTIVE.get("refresher")
    if refresher is not None:
        refresher.schedule(paths)


__all__ = ["configure", "schedule", "start", "start_for_state_db", "stop"]
