"""Background rebuild of on-disk ANLZ JSON caches for ``anlz.cache_rebuild`` jobs."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from apps.analysis import anlz_cache_job
from apps.adapters.rekordbox import config as rb_config
from apps.adapters.rekordbox.paths import resolve_content
from apps.webui.server.rb_vendor_pkg.anlz import build_anlz_payload
from apps.webui.server.rb_vendor_pkg.anlz_cache import all_cached_payloads_fresh

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


def reconcile_from_disk(job: dict[str, Any]) -> str:
    try:
        data_dir, points, stable_ids = anlz_cache_job.parse_payload(job.get("payload") or {})
    except anlz_cache_job.AnlzCacheRebuildPayloadError:
        return "unknown"
    if not stable_ids:
        return "succeeded"
    rb_config.rebind_data_dir(data_dir)
    if all_cached_payloads_fresh(stable_ids, points):
        return "succeeded"
    return "failed"


__all__ = ["ProgressFn", "reconcile_from_disk", "run_rebuild"]
