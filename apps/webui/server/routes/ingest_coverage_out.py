"""The body of GET /ingest/coverage and its cached read (HEALTH-12).

Split out of routes/ingest.py, which holds the route itself: the response
model, the per-app measurement cache and the one function that turns a
measurement (fresh or cached) into the response.
"""
from __future__ import annotations

import threading
from collections.abc import Callable

from fastapi import FastAPI
from pydantic import BaseModel

from apps.webui.server import coverage_cache
from apps.webui.server.routes import ingest_coverage

COVERAGE_CACHED_HELP = (
    "Accept the last measurement instead of waiting for a new one. "
    "`age_s` says how old it is; when `refreshing` is true a newer one "
    "is being taken and a read shortly after gets it. Without this "
    "the library is measured for this request."
)


class CoverageOut(BaseModel):
    """Per-step coverage over ``present`` tracks; see routes/ingest_coverage.py.

    ``on_disk`` is the denominator (``availability.present``). Per step,
    ``done + terminal + failed + pending == on_disk``. ``missing`` and
    ``corrupt`` keep their artifact meaning for the refresh job's targeting:
    ``corrupt`` (structurally invalid entries) is a subset of ``missing``.
    """

    total_tracks: int
    on_disk: int
    unreachable: int
    missing: dict[str, int]
    corrupt: dict[str, int]
    availability: dict[str, int]
    done: dict[str, int]
    terminal: dict[str, int]
    failed: dict[str, int]
    pending: dict[str, int]
    waiting_on_stems: int
    #: Stems ``done`` split (HEALTH-07): on this disk, and only in R2.
    local: dict[str, int]
    in_cloud: dict[str, int]
    #: Vocals that need their bundle fetched from R2 before they can derive.
    awaiting_stem_download: int
    #: ``state`` is ok | off | unknown; unknown renders the stems light grey.
    stems_index: dict[str, str | None]
    stems_source_refusal: str | None
    generated_at: float
    #: Seconds since this measurement was taken; 0 unless ``cached`` was asked.
    age_s: float
    #: A newer measurement is being taken behind a ``cached`` read.
    refreshing: bool
    #: Why the latest background refresh failed. The counts are then the last
    #: good measurement, ``age_s`` old, and must not be shown as current.
    refresh_error: str | None


_cache_lock = threading.Lock()


def cache_for(app: FastAPI) -> coverage_cache.CoverageCache:
    """One cache per app, so a second app in the process shares nothing."""
    with _cache_lock:
        cache = getattr(app.state, "coverage_cache", None)
        if cache is None:
            cache = app.state.coverage_cache = coverage_cache.CoverageCache()
        return cache


def read_coverage(
    app: FastAPI,
    cached: bool,
    build_snapshot: Callable[[FastAPI], ingest_coverage.CoverageSnapshot],
) -> CoverageOut:
    """Measure now, or (``cached``) serve the last measurement with its age."""

    def fields() -> dict[str, object]:
        snapshot = build_snapshot(app)
        return {
            "total_tracks": snapshot.playability.total,
            **ingest_coverage.response_fields(snapshot),
        }

    cache = cache_for(app)
    reading = cache.read(fields) if cached else cache.measure(fields)
    return CoverageOut.model_validate(
        {
            **reading.fields,
            "age_s": reading.age_s,
            "refreshing": reading.refreshing,
            "refresh_error": reading.refresh_error,
        }
    )


__all__ = ["COVERAGE_CACHED_HELP", "CoverageOut", "cache_for", "read_coverage"]
