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

from apps.dedup.db_paths import resolve_dedup_db
from apps.shared import fs_residency, paths
from apps.shared.fingerprints import (
    ChromaprintMissing,
    Fingerprint,
    FingerprintCache,
    compute,
)

from . import find_clusters, tag_clusters
from .find_clusters import _wipe_derived_cluster_tables
from . import schema as dedup_schema

# Each fingerprint is a child process decoding one file; a few at once keep
# the cores busy without starving playback.
DEFAULT_WORKERS = 3
ScanBasis = Literal["fingerprint", "tags", "both"]
DEFAULT_SCAN_BASIS: ScanBasis = "both"


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
    basis: ScanBasis = DEFAULT_SCAN_BASIS
    total: int = 0
    done: int = 0
    computed: int = 0
    cache_hits: int = 0
    # Library rows whose file is not on this machine (missing or a cloud
    # placeholder): skipped, never downloaded.
    not_local: int = 0
    errors: int = 0
    clusters: int | None = None
    clusters_fingerprint: int | None = None
    clusters_tags: int | None = None
    tag_precision: float | None = None
    error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    error_samples: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _tag_precision(state_db: Path, db_path: Path) -> float | None:
    """Share of tag clusters whose members also share one fingerprint cluster."""
    from apps.mik.duplicates import artist_title_duration_groups
    from apps.shared.state.db import open_ro

    state_conn = open_ro(state_db)
    dedup_conn = sqlite3.connect(db_path)
    try:
        fp_buckets: dict[frozenset[str], int] = {}
        for cid, canon in dedup_conn.execute(
            "SELECT cluster_id, canonical_stable_id FROM duplicate_clusters WHERE basis = 'fingerprint'"
        ):
            members = {str(canon)}
            members.update(
                str(row[0])
                for row in dedup_conn.execute(
                    "SELECT alias_stable_id FROM track_aliases WHERE cluster_id = ?", (cid,)
                )
            )
            fp_buckets[frozenset(members)] = int(cid)

        tag_groups = list(artist_title_duration_groups(state_conn))
        if not tag_groups:
            return None
        matched = 0
        eligible = 0
        for group in tag_groups:
            if len(group) < 2:
                continue
            eligible += 1
            group_set = frozenset(group)
            for fp_members in fp_buckets:
                if group_set <= fp_members:
                    matched += 1
                    break
        return matched / eligible if eligible else None
    finally:
        state_conn.close()
        dedup_conn.close()


def run_library_scan(
    *,
    state_db: Path | None = None,
    db_path: Path | None = None,
    workers: int = DEFAULT_WORKERS,
    basis: ScanBasis = DEFAULT_SCAN_BASIS,
    progress: LibraryScanProgress | None = None,
    on_change: Callable[[LibraryScanProgress], None] | None = None,
) -> LibraryScanProgress:
    """Fingerprint and/or tag-cluster the library. Raises ChromaprintMissing when
    the fingerprint pass needs a backend that does not exist."""
    use_state = state_db if state_db is not None else paths.STATE_DB
    use_db = resolve_dedup_db(db_path)
    prog = progress if progress is not None else LibraryScanProgress()
    prog.basis = basis
    lock = threading.Lock()

    def bump(**kw: int) -> None:
        with lock:
            for k, v in kw.items():
                setattr(prog, k, getattr(prog, k) + v)
        if on_change is not None:
            on_change(prog)

    fingerprint_clusters = 0
    tag_cluster_count = 0

    if basis in ("fingerprint", "both"):
        tracks = library_tracks(use_state)
        prog.total = len(tracks)
        cache = FingerprintCache(use_db)
        missing_backend: list[ChromaprintMissing] = []
        results: list[Fingerprint] = []

        def one(item: tuple[str, Path]) -> None:
            sid, path = item
            with lock:
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
                with lock:
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

        by_path = {str(p): sid for sid, p in tracks}
        conn = sqlite3.connect(use_db, timeout=5.0)
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
        fingerprint_clusters = len(outcomes)

    if basis in ("tags", "both"):
        if basis == "tags":
            conn = dedup_schema.ensure_schema(use_db)
            try:
                _wipe_derived_cluster_tables(conn)
                conn.commit()
            finally:
                conn.close()
            prog.total = len(library_tracks(use_state))
        tag_stats = tag_clusters.run_tag_clusters(state_db=use_state, db_path=use_db)
        tag_cluster_count = tag_stats.written
        prog.tag_precision = _tag_precision(use_state, use_db)

    prog.clusters_fingerprint = fingerprint_clusters if basis in ("fingerprint", "both") else 0
    prog.clusters_tags = tag_cluster_count if basis in ("tags", "both") else 0
    prog.clusters = (prog.clusters_fingerprint or 0) + (prog.clusters_tags or 0)
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
            self._progress = LibraryScanProgress(state="running", started_at=_now(), basis=kwargs.get("basis", DEFAULT_SCAN_BASIS))
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
