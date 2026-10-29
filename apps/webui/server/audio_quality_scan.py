"""Scan the library's audio into stored quality facts (QLTSCAN-03).

Every track whose audio is on this machine (`library_playable`'s `present`
list) is decoded once by `apps.shared.audio_scan.scan_file` and its facts are
stored in `audio_quality_store`. Same shape as `grid_quality_scan`.

COST. One streamed ffmpeg decode per file, about 0.2 s per 30 s of audio on
nucbox (so roughly 2.5 s for a 6-minute track). That is minutes to hours for
a whole library, which is why this is:

- INCREMENTAL. A file whose path, mtime and size match the stored row, under
  the same scanner rule, is skipped after one `stat`.
- RESUMABLE. Facts are committed every `WRITE_BATCH` files, and `stop()` ends
  the scan after the files in flight. The next scan picks up where it left
  off because everything already stored is skipped.
- POLITE. ffmpeg runs under `nice -n 10` (below-normal priority on Windows),
  the background scan uses one worker, and it pauses between files.

It runs on its own thread (`ScanRunner`, one per app), started by
`POST /api/v1/audio-quality/scan` or `opendj track quality-scan`, never by a
list request.

MINI-PRD
--------
  ✔︎ ✅ 🎯 scan_library(): facts per present track, persisted in batches.
    [if] a file did not change since the last scan [then] it is not decoded
    [if] the file changed or the scanner rule changed [then] it is re-scanned
    [if] a file cannot be decoded [then] its row records the error and the
         scan goes on ⛔️ a crash that ends the scan, or a letter that reads checked
    [if] stop() is called [then] the scan ends after the files in flight and
         a later scan resumes ⛔️ redoing stored work
  ✔︎ ✅ 🎯 ScanRunner: single-flight background scan with a readable status.

-Claude
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from apps.adapters.rekordbox.errors import _open_ro
from apps.shared.audio_scan import ScanCancelled, ScanError, rule_version, scan_file
from apps.shared.events import publish
from apps.webui.server import audio_quality_store, library_playable
from apps.webui.server.audio_quality_store import StoredScan

log = logging.getLogger(__name__)

#: Seconds slept after each file when the scan runs in the background.
BACKGROUND_PAUSE_S = 0.05
#: Facts are committed in batches, so a stopped or crashed scan keeps its work.
WRITE_BATCH = 20
MAX_WORKERS = 8
SCAN_THREAD_NAME = "audio-quality-scan"
SHUTDOWN_JOIN_S = 5.0


@dataclass(frozen=True)
class ScanResult:
    #: Tracks whose audio is on this machine.
    considered: int
    #: Files decoded this run (the expensive part).
    scanned: int
    #: Files whose stored facts were still current.
    unchanged: int
    #: Files that could not be scanned (stored with their error).
    failed: int
    #: Unreadable files whose stored row changed this run (new, was readable,
    #: or a different error). A file that stays unreadable is not counted.
    newly_unreadable: int
    #: True when stop() ended the run early.
    stopped: bool
    duration_s: float
    finished_at: str


# --------------------------------------------------------------- helpers


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def present_tracks(state_db: Path) -> list[tuple[str, str]]:
    """`(stable_id, local path)` for every track whose audio is on this machine."""
    conn = _open_ro(state_db, "STATE_DB")
    try:
        return list(library_playable.scan_playability(conn).present)
    finally:
        conn.close()


def file_identity(path: str) -> tuple[str, int] | str:
    """`(file_version, size)` from one stat, or the error text when it cannot be read."""
    try:
        stat = Path(path).stat()
    except OSError as exc:
        log.warning("audio-quality: cannot stat %s (%s)", path, exc)
        return f"file unreadable: {exc}"[:300]
    digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]
    return f"{digest}:{stat.st_mtime_ns}:{stat.st_size}", stat.st_size


def scan_one(
    stable_id: str, path: str, version: str, size: int, rule: str,
    cancel: threading.Event | None = None,
) -> StoredScan:
    """Scan one file into a row. A file that will not decode is a row with
    its error, never an exception that ends the library scan. A cancel
    raises ScanCancelled, so a stopped file stores nothing."""
    facts_json: str | None = None
    error: str | None = None
    try:
        facts_json = _dumps(scan_file(Path(path), cancel=cancel).as_dict())
    except ScanError as exc:
        error = str(exc)[:300]
        log.warning("audio-quality: %s: %s", path, error)
    return StoredScan(
        stable_id=stable_id, file_path=path, file_version=version, file_size=size,
        rule_version=rule, facts_json=facts_json, error=error, computed_at=_utc_now(),
    )


def _dumps(facts: dict[str, Any]) -> str:
    return json.dumps(facts, sort_keys=True)


def _unreadable_row(stable_id: str, path: str, error: str, rule: str) -> StoredScan:
    """A file that vanished or cannot be stat-ed between the walk and the scan:
    recorded as a failure, so its letter goes back to unchecked."""
    return StoredScan(
        stable_id=stable_id, file_path=path, file_version="unreadable", file_size=-1,
        rule_version=rule, facts_json=None, error=error, computed_at=_utc_now(),
    )


def _todo(
    tracks: Iterable[tuple[str, str]], stored: dict[str, StoredScan], rule: str
) -> tuple[list[tuple[str, str, str, int]], int, list[StoredScan]]:
    todo: list[tuple[str, str, str, int]] = []
    unreadable: list[StoredScan] = []
    unchanged = 0
    for stable_id, path in tracks:
        identity = file_identity(path)
        if isinstance(identity, str):
            unreadable.append(_unreadable_row(stable_id, path, identity, rule))
            continue
        version, size = identity
        previous = stored.get(stable_id)
        if previous is not None and previous.file_version == version and previous.rule_version == rule:
            unchanged += 1
            continue
        todo.append((stable_id, path, version, size))
    return todo, unchanged, unreadable


def _row_changed(previous: StoredScan | None, row: StoredScan) -> bool:
    """True when storing `row` changes what a listing shows for its track."""
    return previous is None or (previous.facts_json, previous.error) != (row.facts_json, row.error)


# ------------------------------------------------------------------ scan


def scan_library(
    state_db: Path,
    *,
    workers: int = 1,
    pause_s: float = 0.0,
    limit: int | None = None,
    stop: threading.Event | None = None,
) -> ScanResult:
    """Bring the stored facts of every present track up to date.

    `limit` caps how many files are decoded this run (the rest wait for the
    next one). `stop`, when set, ends the run after the files in flight."""
    if not 1 <= workers <= MAX_WORKERS:
        raise ValueError(f"workers must be 1..{MAX_WORKERS}, got {workers}")
    started = time.perf_counter()
    rule = rule_version()
    tracks = present_tracks(state_db)
    stored = audio_quality_store.read_many([sid for sid, _path in tracks], state_db)
    todo, unchanged, unreadable = _todo(tracks, stored, rule)
    newly_unreadable = sum(_row_changed(stored.get(row.stable_id), row) for row in unreadable)
    audio_quality_store.upsert_many(unreadable, state_db)
    if limit is not None:
        todo = todo[:limit]
    stop = stop or threading.Event()
    scanned = 0
    failed = len(unreadable)
    pending: list[StoredScan] = []

    def work(item: tuple[str, str, str, int]) -> StoredScan | None:
        if stop.is_set():
            return None
        try:
            row = scan_one(item[0], item[1], item[2], item[3], rule, cancel=stop)
        except ScanCancelled:
            return None
        if pause_s > 0:
            time.sleep(pause_s)
        return row

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=SCAN_THREAD_NAME) as pool:
        for row in pool.map(work, todo):
            if row is None:
                continue
            scanned += 1
            failed += row.error is not None
            pending.append(row)
            if len(pending) >= WRITE_BATCH:
                audio_quality_store.upsert_many(pending, state_db)
                pending = []
    audio_quality_store.upsert_many(pending, state_db)
    return ScanResult(
        considered=len(tracks),
        scanned=scanned,
        unchanged=unchanged,
        failed=failed,
        newly_unreadable=newly_unreadable,
        stopped=stop.is_set(),
        duration_s=round(time.perf_counter() - started, 3),
        finished_at=_utc_now(),
    )


# ---------------------------------------------------------------- runner


class ScanRunner:
    """Single-flight scan with a status any caller can read, and a stop."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._running = False
        self._stop = threading.Event()
        self._last_result: ScanResult | None = None
        self._last_error: str | None = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": "running" if self._running else "idle",
                "stopping": self._running and self._stop.is_set(),
                "last_result": asdict(self._last_result) if self._last_result else None,
                "last_error": self._last_error,
            }

    def _claim(self) -> bool:
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._stop = threading.Event()
            return True

    def _run(self, state_db: Path, workers: int, pause_s: float, limit: int | None) -> None:
        result: ScanResult | None = None
        error: str | None = None
        try:
            result = scan_library(state_db, workers=workers, pause_s=pause_s, limit=limit, stop=self._stop)
        except Exception as exc:
            log.exception("audio-quality scan failed")
            error = f"{type(exc).__name__}: {exc}"
        with self._lock:
            self._running = False
            self._last_error = error
            if result is not None:
                self._last_result = result
        if result is not None and (result.scanned > 0 or result.newly_unreadable > 0):
            # Rows carry the grade, so listings must be read again. A file that
            # just became unreadable changes its row too (checked -> failed).
            publish("library.changed", {"kind": "tracks", "ids": []})

    def run_blocking(self, state_db: Path, workers: int, limit: int | None) -> dict[str, Any]:
        """Scan on the caller's thread; refuses while a scan is running."""
        if not self._claim():
            return self.status()
        self._run(state_db, workers, 0.0, limit)
        return self.status()

    def start(self, state_db: Path, limit: int | None = None) -> dict[str, Any]:
        """Start a background scan (one worker) unless one is running."""
        if self._claim():
            thread = threading.Thread(
                target=self._run, args=(state_db, 1, BACKGROUND_PAUSE_S, limit),
                name=SCAN_THREAD_NAME, daemon=True,
            )
            with self._lock:
                self._thread = thread
            thread.start()
        return self.status()

    def stop(self) -> dict[str, Any]:
        """Ask a running scan to end after the files in flight."""
        self._stop.set()
        return self.status()

    def shutdown(self) -> None:
        """App shutdown: stop queued files, kill the decode in flight, and wait
        a bounded time. The thread is a daemon, so a slow exit ends with the process."""
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(SHUTDOWN_JOIN_S)
            if thread.is_alive():
                log.warning("audio-quality scan still stopping after %s s at shutdown", SHUTDOWN_JOIN_S)

    def join(self, timeout_s: float) -> None:
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout_s)
            if thread.is_alive():
                raise TimeoutError(f"audio-quality scan still running after {timeout_s} s")


_RUNNER_GUARD = threading.Lock()


def runner_for(app_state: Any) -> ScanRunner:
    """The one runner of a FastAPI app (`app.state`), created on first use."""
    with _RUNNER_GUARD:
        runner = getattr(app_state, "audio_quality_runner", None)
        if runner is None:
            runner = ScanRunner()
            app_state.audio_quality_runner = runner
        return runner


__all__ = [
    "BACKGROUND_PAUSE_S",
    "MAX_WORKERS",
    "ScanResult",
    "ScanRunner",
    "file_identity",
    "present_tracks",
    "runner_for",
    "scan_library",
    "scan_one",
]
