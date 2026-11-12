"""Health endpoint (CAT-05)."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

import anyio
from anyio import to_thread
from fastapi import APIRouter, Depends, HTTPException, Request, status

from apps.shared.app_version import APP_VERSION

from .. import rb_vendor
from ..auth import GoogleOAuthConfig
from ..backend import StateBackend
from ..deps import get_read_state
from ..models import (
    HealthCloud,
    HealthOut,
    HealthStateDb,
    HealthSyncthing,
    HealthWaveformMaterialization,
)
from ..ops32_env_guard import OPS32_FORBIDDEN_ENV_PREFIXES, OPS32_HOME_ENV_VAR
from ..state_paths import resolve_state_db_path
from ..ui_url import resolve_ui_address

router = APIRouter(prefix="/health", tags=["health"])
MOUNT_UNDER_API_PREFIX = True

# HEALTH-POOL-01 (issue reported Mon 5 Oct 2026, silver preview): /health is
# a liveness probe and must answer regardless of how busy the shared AnyIO
# sync-route threadpool is. FastAPI/Starlette dispatches every plain `def`
# route handler onto ONE shared threadpool gated by
# anyio.to_thread.current_default_thread_limiter() (default 40 tokens).
# GET /api/v1/commands/next is polled every 50ms per open tab, and library
# walks / coverage sweeps also run as sync handlers on that same pool; once
# every token is in use, anything else dispatched there -- including this
# probe, when it was also a plain `def` -- queues behind them too. Measured
# on the silver preview: GET /api/v1/health took >8s three times
# (20:46:48Z, 20:47:45Z, 20:49:09Z) while GET /api/v1/tracks answered fine
# in the same window, and a standalone call to /health in isolation
# returned in 12ms -- so the handler itself was never slow, only queued.
#
# Two-layered fix:
#   1. `health` is `async def`, so FastAPI never has to wait for a shared
#      threadpool token just to start running it.
#   2. The one call inside it that is genuinely blocking (`backend.stats()`,
#      a sqlite read) is dispatched through a SEPARATE, dedicated capacity
#      limiter (`_HEALTH_THREAD_LIMITER`) instead of the shared default
#      one, so a saturated pool of sync endpoints can never make this probe
#      wait for a token. A short `fail_after` bounds how long a genuinely
#      wedged DB can hold the probe; past that it reports 503 rather than
#      hanging or fabricating a result (no silent fallback).
_HEALTH_THREAD_LIMITER = anyio.CapacityLimiter(4)
_HEALTH_DB_TIMEOUT_S = 2.0
_HEALTH_OPTIONAL_PROBE_TIMEOUT_S = 1.5
# The dev-frontend probe in resolve_ui_address is a blocking HTTP read of up
# to 1.5 s against a stalled listener. It runs off the event loop on the
# dedicated limiter, and past this bound ui_url reports None (not served), so
# /health keeps its one-second contract (discussion_r4251103691).
_HEALTH_UI_URL_TIMEOUT_S = 0.5


async def _call_optional_status_fn(fn: Callable[[], Any] | None) -> Any | None:
    """Run an optional, best-effort status probe (cloud lock / Syncthing) off
    the shared threadpool, on its own short timeout.

    These two fields have always been best-effort: the pre-existing contract
    (see the HealthCloud/HealthSyncthing callers below) is that a probe
    failure reports "not available", never a failed health check. A probe
    that merely times out gets the same treatment -- the liveness verdict
    itself depends only on ``backend.stats()`` below, not on an optional
    enrichment call that could itself be arbitrary blocking I/O (e.g. the
    Syncthing REST probe in cloud_sync.py uses blocking urllib).
    """
    if fn is None:
        return None
    try:
        with anyio.fail_after(_HEALTH_OPTIONAL_PROBE_TIMEOUT_S):
            return await to_thread.run_sync(fn, limiter=_HEALTH_THREAD_LIMITER)
    except Exception:  # noqa: BLE001  # pragma: no cover -- best-effort probe
        return None


@router.get("", response_model=HealthOut)
async def health(
    request: Request,
    backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
) -> HealthOut:
    try:
        with anyio.fail_after(_HEALTH_DB_TIMEOUT_S):
            stats = await to_thread.run_sync(
                backend.stats,
                limiter=_HEALTH_THREAD_LIMITER,
            )
    except TimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "state_db_timeout",
                "message": f"state.db did not answer within {_HEALTH_DB_TIMEOUT_S}s",
            },
        ) from exc
    last_writer = backend.last_writer()
    # PREFLIGHT-02 (#2589): the same resolver GET /api/v1/preflight calls, so
    # the two endpoints cannot report two different files under one name.
    db_path = resolve_state_db_path(request)
    bind_host = getattr(request.app.state, "bind_host", "127.0.0.1")
    version = getattr(request.app.state, "version", APP_VERSION)

    lock_fn: Callable[[], Any] | None = getattr(request.app.state, "lock_status_fn", None)
    lock_holder = await _call_optional_status_fn(lock_fn)
    cloud = HealthCloud(lock_holder=lock_holder)

    syncthing_fn: Callable[[], Any] | None = getattr(request.app.state, "syncthing_status_fn", None)
    syncthing = await _call_optional_status_fn(syncthing_fn)

    return HealthOut(
        status="ok",
        state_db=HealthStateDb(
            path=str(db_path),
            tracks=stats["tracks"],
            tracks_playable=int(stats.get("tracks_playable", 0)),
            playlists=stats["playlists"],
            pairings=stats["pairings"],
            last_writer_hostname=last_writer[0] if last_writer else None,
            last_writer_at=last_writer[1] if last_writer else None,
        ),
        cloud=cloud,
        syncthing=(HealthSyncthing(**syncthing) if isinstance(syncthing, dict) else None),
        waveform_materialization=HealthWaveformMaterialization(**rb_vendor.waveform_materialization_status()),
        bind_host=bind_host,
        version=version,
        ui_url=await _ui_url(request),
        google_oauth_configured=GoogleOAuthConfig.is_configured(dict(os.environ)),
        # OPS-32 round 4: narrowed from every env key name (round 3) to just
        # the ones matching a forbidden prefix, plus one control bit -- round
        # 3's full key list was readable by an unauthenticated caller on a
        # token-mode share host, since /health is exempt from that gate so
        # cloudflared can probe it pre-auth (apps/webui/server/share_gate.py
        # EXEMPT_SUFFIXES). Names only, never values, and never the harmless
        # majority of the environment.
        process_env_forbidden_keys=sorted(key for key in os.environ if key.startswith(OPS32_FORBIDDEN_ENV_PREFIXES)),
        process_env_home_present=OPS32_HOME_ENV_VAR in os.environ,
    )


def bug_report_health_summary(health: HealthOut) -> dict[str, object]:
    """Counts and flags only: never the state-db path (issue #11173)."""
    return {
        "status": health.status,
        "state_db": {
            "tracks": health.state_db.tracks,
            "tracks_playable": health.state_db.tracks_playable,
            "playlists": health.state_db.playlists,
            "pairings": health.state_db.pairings,
            "last_writer_hostname": health.state_db.last_writer_hostname,
            "last_writer_at": health.state_db.last_writer_at,
        },
        "bind_host": health.bind_host,
        "version": health.version,
        "ui_url": health.ui_url,
        "google_oauth_configured": health.google_oauth_configured,
        "process_env_home_present": health.process_env_home_present,
        "process_env_forbidden_key_count": len(health.process_env_forbidden_keys),
        "waveform_materialization": health.waveform_materialization.model_dump(),
        "cloud_lock_holder_present": health.cloud.lock_holder is not None,
        "syncthing": (health.syncthing.model_dump() if health.syncthing is not None else None),
    }


async def _ui_url(request: Request) -> str | None:
    try:
        with anyio.fail_after(_HEALTH_UI_URL_TIMEOUT_S):
            address = await to_thread.run_sync(
                resolve_ui_address,
                request.app.state,
                limiter=_HEALTH_THREAD_LIMITER,
                abandon_on_cancel=True,
            )
    except TimeoutError:
        return None
    return None if address is None else address.ui_url
