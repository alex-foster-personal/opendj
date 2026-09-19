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

from apps.adapters.rekordbox.paths import resolve_asset_path
from apps.shared.state import db as state_db
from apps.shared.state import path_availability as path_index

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
        from apps.webui.server.rb_vendor_pkg import track_rows

        namespace = path_index.resolver_namespace(self._data_dir)
        rows: list[tuple[str, int | None]] = []
        for path in paths:
            mapped = resolve_asset_path(path)
            rows.append((path, track_rows._stat_size(mapped.resolved)))
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


_refresher: PathAvailabilityRefresher | None = None


def configure(*, data_dir: Path, state_db_path: Path) -> None:
    global _refresher
    _refresher = PathAvailabilityRefresher(
        data_dir=data_dir,
        state_db_path=state_db_path,
    )


def start() -> None:
    if _refresher is None:
        raise RuntimeError("path availability refresher is not configured")
    _refresher.start()


def stop() -> None:
    if _refresher is not None:
        _refresher.stop()


def schedule(paths: Iterable[str]) -> None:
    if _refresher is None:
        return
    _refresher.schedule(paths)


__all__ = ["configure", "schedule", "start", "stop"]
