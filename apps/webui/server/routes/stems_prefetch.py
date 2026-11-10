"""Request-bound deps for the next-up stem-set prefetch (STEM-11747).

Keeps request plumbing out of ``apps.cloud`` and out of ``ahead_analysis.py``:
the PUT next-up route binds these to its request and hands them to
:class:`apps.cloud.stem_hydration.NextUpStemPrefetcher`, whose worker thread
runs them, never the event loop.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException, Request

from apps.cloud import stem_cache_budget, stem_hydration
from apps.cloud.stem_source import StemSourceError

from .stems_bundle import _stems_dir
from .stems_hydration import _enqueue_hydration


def stem_prefetch_deps(request: Request) -> stem_hydration.PrefetchDeps:
    return stem_hydration.PrefetchDeps(
        state_of=lambda sid: _stem_state_name(sid, request),
        floor_refusal=lambda: _cache_floor_refusal(request),
        hydrate=lambda sid: _prefetch_hydration(sid, request),
    )


def _stem_state_name(stable_id: str, request: Request) -> str:
    from .stems import _stem_state  # stems.py mounts the router this module serves

    return _stem_state(stable_id, request).state


def _cache_floor_refusal(request: Request) -> str | None:
    """Why the cache is under the STEM-39 floor, or None when there is room."""
    settings = stem_cache_budget.load_settings(Path(request.app.state.stem_hydration_data_dir))
    usage = stem_cache_budget.measure_disk(_stems_dir(request))
    floor = stem_cache_budget.floor_bytes(usage.total_bytes, settings)
    if usage.free_bytes < floor:
        return f"free {usage.free_bytes} bytes < floor {floor} bytes"
    return None


def _prefetch_hydration(stable_id: str, request: Request) -> stem_hydration.HydrationOutcome:
    try:
        future = _enqueue_hydration(stable_id, request, prefetch=True)
    except HTTPException as exc:
        if isinstance(exc.detail, dict):
            raise StemSourceError(exc.detail["code"], exc.detail["message"]) from exc
        raise
    if future is None:
        return stem_hydration.HydrationOutcome(stable_id, "unavailable", reason="stem hydration is not configured here")
    return future.result()
