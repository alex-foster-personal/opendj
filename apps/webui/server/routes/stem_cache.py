"""Stem cache disk budget: status, enforce-now and settings (STEM-43).

Agent-native parity for the low-disk indicator in the UI health area and for
``python -m apps.stems cache-status | cache-enforce | cache-settings``. Its
own module rather than more routes on ``routes/stems.py`` (the track-scoped
loader) or ``routes/stems_assets.py`` (the migration rail).

``GET /stems/cache/status`` never writes and never hashes, so a health dot
can poll it. ``POST /stems/cache/enforce`` is the same pass the engine's
timer runs, on demand; ``dry_run`` reports the plan and removes nothing.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from apps.cloud import stem_cache_budget, stem_index
from apps.webui.server import stem_cache_enforcer

router = APIRouter(tags=["stems"])

STEM_CACHE_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    422: {
        "description": (
            "STEM_CACHE_SETTINGS_INVALID: the stored or submitted settings are "
            "malformed; nothing was evicted or saved"
        )
    },
    502: {
        "description": (
            "STEM_INDEX_CORRUPT: the local stem index cache is present but "
            "unreadable, so no bundle can be confirmed against R2"
        )
    },
}


class StemCacheSettingsOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    floor_gib: float = Field(description="Absolute free-space floor, GiB")
    floor_fraction: float = Field(description="Free-space floor as a fraction of the volume")
    max_cache_gib: float | None = Field(description="Optional hard cap on the cache, GiB")
    enforce_interval_s: float = Field(description="Seconds between engine enforcement ticks")
    auto_evict: bool = Field(description="Whether low disk evicts R2-confirmed bundles")


class StemCacheSettingsIn(BaseModel):
    """Partial update: omitted fields keep their stored value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    floor_gib: float | None = None
    floor_fraction: float | None = None
    max_cache_gib: float | None = None
    clear_max_cache_gib: bool = False
    enforce_interval_s: float | None = None
    auto_evict: bool | None = None


class StemCacheEnforcementOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    at_utc: str
    state: Literal["healthy", "low_disk"]
    dry_run: bool
    disk_total_bytes: int
    disk_free_bytes: int
    floor_bytes: int
    shortfall_bytes: int
    cache_bytes: int
    budget_bytes: int
    evicted_stable_ids: list[str]
    bytes_freed: int
    queued_for_upload: list[str]
    protected_count: int
    blocked_reason: str | None


class StemCacheStatusOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    state: Literal["healthy", "low_disk"]
    stems_dir: str
    disk_total_bytes: int
    disk_free_bytes: int
    floor_bytes: int
    shortfall_bytes: int = Field(description="Bytes short of the floor; 0 when healthy")
    cache_bytes: int
    budget_bytes: int = Field(description="Largest cache that still leaves the floor free")
    over_budget_bytes: int
    bundle_count: int
    evictable_bundle_count: int = Field(description="In the R2 index and not on a deck")
    evictable_bytes: int
    would_evict_count: int = Field(
        description="Least-recently-used bundles that reaching the floor would remove; "
        "each stays in R2 and is fetched back on demand"
    )
    would_evict_bytes: int
    local_only_count: int = Field(description="Not covered by the R2 index; never evicted")
    local_only_bytes: int
    local_only_stable_ids: list[str]
    upload_queue_count: int
    protected_count: int = Field(description="Loaded or playing; never evicted")
    can_rehydrate: bool = Field(description="Whether a hydration source is armed")
    blocked_reason: str | None = Field(
        description="Why low disk is not being relieved, or null"
    )
    settings: StemCacheSettingsOut
    enforcer_running: bool
    last_enforcement: StemCacheEnforcementOut | None
    last_error: str | None


class StemCacheEnforceIn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    dry_run: bool = False


def _settings_invalid(exc: stem_cache_budget.StemCacheSettingsError) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"code": "STEM_CACHE_SETTINGS_INVALID", "message": str(exc)},
    )


def _index_corrupt(exc: stem_index.StemIndexError) -> HTTPException:
    return HTTPException(
        status_code=502, detail={"code": "STEM_INDEX_CORRUPT", "message": str(exc)}
    )


def _enforcement_out(report: stem_cache_budget.EnforceReport) -> StemCacheEnforcementOut:
    return StemCacheEnforcementOut(**report.as_dict())


@router.get(
    "/stems/cache/status",
    response_model=StemCacheStatusOut,
    responses=STEM_CACHE_ERROR_RESPONSES,
)
def get_stem_cache_status(request: Request) -> StemCacheStatusOut:
    """Where the stem cache stands against the free-disk floor. Read-only."""
    app = request.app
    try:
        inputs = stem_cache_enforcer.cache_inputs(app)
        payload = stem_cache_budget.status(
            inputs.stems_dir,
            data_dir=inputs.data_dir,
            index=inputs.index,
            protected=inputs.protected,
            can_rehydrate=inputs.can_rehydrate,
        )
    except stem_cache_budget.StemCacheSettingsError as exc:
        raise _settings_invalid(exc) from exc
    except stem_index.StemIndexError as exc:
        raise _index_corrupt(exc) from exc
    enforcer = getattr(app.state, "stem_cache_enforcer", None)
    last = enforcer.last_report if enforcer is not None else None
    return StemCacheStatusOut(
        **payload,
        enforcer_running=bool(enforcer is not None and enforcer.running),
        last_enforcement=_enforcement_out(last) if last is not None else None,
        last_error=enforcer.last_error if enforcer is not None else None,
    )


@router.post(
    "/stems/cache/enforce",
    response_model=StemCacheEnforcementOut,
    responses=STEM_CACHE_ERROR_RESPONSES,
)
def enforce_stem_cache(request: Request, body: StemCacheEnforceIn) -> StemCacheEnforcementOut:
    """Run the engine's enforcement pass now. Evicts only bundles the R2
    index holds byte for byte, least recently used first, and stops at the
    floor; ``dry_run`` reports the plan without removing anything."""
    try:
        report = stem_cache_enforcer.enforce_for_app(request.app, dry_run=body.dry_run)
    except stem_cache_budget.StemCacheSettingsError as exc:
        raise _settings_invalid(exc) from exc
    except stem_index.StemIndexError as exc:
        raise _index_corrupt(exc) from exc
    enforcer = getattr(request.app.state, "stem_cache_enforcer", None)
    if enforcer is not None and not body.dry_run:
        enforcer.last_report = report
    return _enforcement_out(report)


@router.get(
    "/stems/cache/settings",
    response_model=StemCacheSettingsOut,
    responses=STEM_CACHE_ERROR_RESPONSES,
)
def get_stem_cache_settings(request: Request) -> StemCacheSettingsOut:
    data_dir = stem_cache_enforcer.cache_data_dir(request.app)
    try:
        return StemCacheSettingsOut(**asdict(stem_cache_budget.load_settings(data_dir)))
    except stem_cache_budget.StemCacheSettingsError as exc:
        raise _settings_invalid(exc) from exc


@router.put(
    "/stems/cache/settings",
    response_model=StemCacheSettingsOut,
    responses=STEM_CACHE_ERROR_RESPONSES,
)
def put_stem_cache_settings(request: Request, body: StemCacheSettingsIn) -> StemCacheSettingsOut:
    """Override the floor, the optional cap, the tick interval or auto-evict.
    Omitted fields keep their stored value; ``clear_max_cache_gib`` removes
    the cap (``null`` cannot, because it already means "leave unchanged")."""
    data_dir = stem_cache_enforcer.cache_data_dir(request.app)
    try:
        merged = stem_cache_budget.merged_settings(
            stem_cache_budget.load_settings(data_dir),
            body.model_dump(exclude={"clear_max_cache_gib"}, exclude_none=True),
            clear_max_cache_gib=body.clear_max_cache_gib,
        )
        stem_cache_budget.save_settings(data_dir, merged)
    except stem_cache_budget.StemCacheSettingsError as exc:
        raise _settings_invalid(exc) from exc
    return StemCacheSettingsOut(**asdict(merged))


__all__ = ["router"]
