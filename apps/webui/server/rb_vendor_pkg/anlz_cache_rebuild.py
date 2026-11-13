"""Background rebuild of on-disk ANLZ JSON caches for ``anlz.cache_rebuild`` jobs."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Callable

from apps.analysis import anlz_cache_job
from apps.adapters.rekordbox import config as rb_config
from apps.adapters.rekordbox.content_paths import resolve_asset_path
from apps.adapters.rekordbox.paths import resolve_content
from apps.webui.server.rb_vendor_pkg.anlz import build_anlz_payload

ProgressFn = Callable[[float, str], None]


def run_rebuild(
    *,
    data_dir: Path,
    points: int,
    stable_ids: list[str],
    on_progress: ProgressFn | None = None,
) -> None:
    """Walk ``stable_ids`` and write schema-versioned anlz-cache entries."""
    rb_config.rebind_data_dir(data_dir)
    state_db = rb_config.STATE_DB
    total = len(stable_ids)
    if total == 0:
        if on_progress is not None:
            on_progress(1.0, "no rekordbox ANLZ sources to rebuild")
        return

    def _emit(progress: float, message: str) -> None:
        if on_progress is not None:
            on_progress(progress, message)

    _emit(0.0, f"rebuilding anlz cache for {total} track(s)")
    failures: list[str] = []
    for index, stable_id in enumerate(stable_ids, start=1):
        try:
            content = resolve_content(stable_id)
            build_anlz_payload(content, points, state_db_path=state_db)
        except Exception as exc:  # noqa: BLE001 -- collect per-track failures
            failures.append(f"{stable_id}: {exc}")
        frac = index / total
        _emit(frac, f"rebuilt {index}/{total}")
    if failures:
        sample = "; ".join(failures[:3])
        more = f" (+{len(failures) - 3} more)" if len(failures) > 3 else ""
        raise RuntimeError(f"anlz cache rebuild failed for {len(failures)} track(s): {sample}{more}")
    _emit(1.0, "succeeded")


def _open_ro(path: Path) -> sqlite3.Connection:
    """Open one library database read-only by explicit path."""
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _anlz_mtime_at(analysis_data_path: str) -> float | None:
    """Newest ANLZ file mtime beside ``analysis_data_path``, or None when absent."""
    mapped = resolve_asset_path(analysis_data_path)
    if mapped.resolved is None or not mapped.resolved.is_file():
        return None
    files = sorted(mapped.resolved.parent.glob("ANLZ*"))
    if not files:
        return None
    return max(f.stat().st_mtime for f in files)


def _entry_fresh(cache_dir: Path, stable_id: str, anlz_mtime: float, points: int) -> bool:
    """Whether ``cache_dir`` holds a current, readable entry for ``stable_id`` (read only)."""
    try:
        cached = json.loads((cache_dir / f"{stable_id}.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not (
        isinstance(cached, dict)
        and cached.get("schema") == rb_config.ANLZ_CACHE_SCHEMA
        and cached.get("anlz_mtime") == anlz_mtime
        and cached.get("points") == points
    ):
        return False
    payload = cached.get("payload")
    if not isinstance(payload, dict):
        return False
    unreadable = payload.get("unreadable_anlz")
    return not (isinstance(unreadable, list) and unreadable)


def caches_fresh_in(data_dir: Path, stable_ids: list[str], points: int) -> bool:
    """Check ``data_dir``'s anlz-cache by explicit paths, never rebinding daemon config."""
    state_path = data_dir / "state" / "state.db"
    master_path = data_dir / "master.plain.db"
    cache_dir = data_dir / "state" / "anlz-cache"
    if not state_path.is_file() or not master_path.is_file():
        return False
    state = _open_ro(state_path)
    master = _open_ro(master_path)
    try:
        for stable_id in stable_ids:
            vendor = state.execute(
                "SELECT t.stable_id, v.vendor_id FROM tracks t "
                "JOIN track_vendor_ids v ON v.stable_id = t.stable_id "
                "WHERE t.stable_id = ? AND t.deleted_at IS NULL "
                "AND v.vendor = 'rekordbox' AND v.deleted_at IS NULL",
                (stable_id,),
            ).fetchone()
            if vendor is None:
                return False
            row = master.execute(
                "SELECT AnalysisDataPath FROM djmdContent WHERE ID = ? AND rb_local_deleted = 0",
                (str(vendor[1]),),
            ).fetchone()
            if row is None or not row[0]:
                return False
            mtime = _anlz_mtime_at(str(row[0]))
            if mtime is None or not _entry_fresh(cache_dir, stable_id, mtime, points):
                return False
        return True
    except sqlite3.Error:
        return False
    finally:
        master.close()
        state.close()


def reconcile_from_disk(job: dict[str, Any]) -> str:
    """Judge a lost rebuild job from its library's cache on disk, without touching daemon globals."""
    try:
        data_dir, points, stable_ids = anlz_cache_job.parse_payload(job.get("payload") or {})
    except anlz_cache_job.AnlzCacheRebuildPayloadError:
        return "unknown"
    if not stable_ids:
        return "succeeded"
    if caches_fresh_in(data_dir, stable_ids, points):
        return "succeeded"
    return "failed"


__all__ = ["ProgressFn", "caches_fresh_in", "reconcile_from_disk", "run_rebuild"]
