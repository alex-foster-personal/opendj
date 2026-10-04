"""Fingerprint the tracks in the library and group the duplicates.

``python -m apps.dedup.scan`` walks folders; this walks the library itself
(``tracks`` in state.db), so it is what the app runs from the duplicate
review page: every live track with a local file is fingerprinted by the
engine (``odj-audio fingerprint``, nothing leaves the machine), cached by
``(path, size, mtime)`` in the dedup database under the TRACK's own
``stable_id``, and then :func:`apps.dedup.find_clusters.run_find_clusters`
groups them over this library's fingerprints only.

Read-only towards the user's music: files are opened for decoding and never
written, moved or deleted. A file whose bytes are not on this machine (a
cloud placeholder) is skipped rather than read, because reading one would
download it.
"""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from apps.shared import fs_residency, paths
from apps.shared.fingerprints import (
    ChromaprintMissing,
    Fingerprint,
    FingerprintCache,
    compute,
)

from . import find_clusters

# Each fingerprint is a child process decoding one file; a few at once keep
# the cores busy without starving playback.
DEFAULT_WORKERS = 3


def library_tracks(state_db: Path) -> list[tuple[str, Path]]:
    """``(stable_id, path)`` of every live track that names a file."""
    from apps.shared.state.db import open_ro

    conn = open_ro(state_db)
    try:
        rows = conn.execute(
            "SELECT stable_id, file_path FROM tracks "
            "WHERE file_path IS NOT NULL AND file_path != '' "
            "AND deleted_at IS NULL ORDER BY file_path"
        ).fetchall()
    finally:
        conn.close()
    return [(str(sid), Path(fp)) for sid, fp in rows]


@dataclass
class LibraryScanProgress:
    state: Literal["idle", "running", "done", "failed"] = "idle"
    total: int = 0
    done: int = 0
    computed: int = 0
    cache_hits: int = 0
    # Library rows whose file is not on this machine (missing or a cloud
    # placeholder): skipped, never downloaded.
    not_local: int = 0
    errors: int = 0
    clusters: int | None = None
    error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    error_samples: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def run_library_scan(
    *,
    state_db: Path | None = None,
    db_path: Path | None = None,
    workers: int = DEFAULT_WORKERS,
    progress: LibraryScanProgress | None = None,
    on_change: Callable[[LibraryScanProgress], None] | None = None,
) -> LibraryScanProgress:
    """Fingerprint the library, then cluster. Raises ChromaprintMissing when
    no fingerprint backend exists (nothing could be fingerprinted)."""
    use_state = state_db if state_db is not None else paths.STATE_DB
    use_db = db_path if db_path is not None else paths.DEDUP_FALLBACK_DB
    prog = progress if progress is not None else LibraryScanProgress()
    lock = threading.Lock()

    def bump(**kw: int) -> None:
        with lock:
            for k, v in kw.items():
                setattr(prog, k, getattr(prog, k) + v)
        if on_change is not None:
            on_change(prog)

    tracks = library_tracks(use_state)
    prog.total = len(tracks)
    cache = FingerprintCache(use_db)
    missing_backend: list[ChromaprintMissing] = []
    results: list[Fingerprint] = []

    def one(item: tuple[str, Path]) -> None:
        sid, path = item
        if missing_backend:
            return
        if not fs_residency.is_materialised(path):
            bump(not_local=1, done=1)
            return
        try:
            hit = cache.get(path)
            if hit is not None:
                with lock:
                    results.append(hit)
                bump(cache_hits=1, done=1)
                return
            fp = compute(path)
            cache.put(fp, stable_id=sid)
            with lock:
                results.append(fp)
            bump(computed=1, done=1)
        except ChromaprintMissing as exc:
            missing_backend.append(exc)
        except Exception as exc:  # noqa: BLE001 -- one bad file never stops the scan
            with lock:
                if len(prog.error_samples) < 5:
                    prog.error_samples.append(f"{path.name}: {exc}"[:300])
            bump(errors=1, done=1)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(one, tracks))
    if missing_backend:
        raise missing_backend[0]

    # A cache hit keeps the stable_id it was stored with; re-key rows the
    # library now knows under another id, so clusters name today's tracks.
    by_path = {str(p): sid for sid, p in tracks}
    conn = sqlite3.connect(use_db)
    try:
        conn.executemany(
            "UPDATE fingerprints SET stable_id = ? WHERE path = ? "
            "AND (stable_id IS NULL OR stable_id != ?)",
            [(by_path[str(fp.path)], str(fp.path), by_path[str(fp.path)]) for fp in results],
        )
        conn.commit()
    finally:
        conn.close()

    outcomes = find_clusters.run_find_clusters(db_path=use_db, fingerprints=results)
    prog.clusters = len(outcomes)
    return prog


class LibraryScanJob:
    """One library scan at a time, on a background thread."""

    def __init__(self, runner: Callable[..., LibraryScanProgress] = run_library_scan) -> None:
        self._runner = runner
        self._lock = threading.Lock()
        self._progress = LibraryScanProgress()
        self._thread: threading.Thread | None = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            return self._progress.as_dict()

    def start(self, **kwargs: Any) -> bool:
        """Start a scan; False when one is already running."""
        with self._lock:
            if self._progress.state == "running":
                return False
            self._progress = LibraryScanProgress(state="running", started_at=_now())
            prog = self._progress

        def run() -> None:
            try:
                self._runner(progress=prog, **kwargs)
                prog.state = "done"
            except Exception as exc:  # noqa: BLE001 -- reported through status()
                prog.error = f"{type(exc).__name__}: {exc}"
                prog.state = "failed"
            finally:
                prog.finished_at = _now()

        self._thread = threading.Thread(target=run, name="dedup-library-scan", daemon=True)
        self._thread.start()
        return True

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)


JOB = LibraryScanJob()
