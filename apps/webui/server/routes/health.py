"""Health endpoint (CAT-05)."""
from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, Request

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
from ..ops32_env_guard import OPS32_FORBIDDEN_ENV_PREFIXES
from ..state_paths import resolve_state_db_path

router = APIRouter(prefix="/health", tags=["health"])


@router.get("", response_model=HealthOut)
def health(
    request: Request,
    backend: StateBackend = Depends(get_read_state),  # noqa: B008  # FastAPI DI
) -> HealthOut:
    stats = backend.stats()
    last_writer = backend.last_writer()
    # PREFLIGHT-02 (#2589): the same resolver GET /api/v1/preflight calls, so
    # the two endpoints cannot report two different files under one name.
    db_path = resolve_state_db_path(request)
    bind_host = getattr(request.app.state, "bind_host", "127.0.0.1")
    version = getattr(request.app.state, "version", "0.1.0")

    lock_fn: Callable[[], Any] | None = getattr(
        request.app.state, "lock_status_fn", None
    )
    lock_holder = None
    if lock_fn is not None:
        try:
            lock_holder = lock_fn()
        except Exception:  # pragma: no cover
            lock_holder = None
    cloud = HealthCloud(lock_holder=lock_holder)

    syncthing_fn: Callable[[], Any] | None = getattr(
        request.app.state, "syncthing_status_fn", None
    )
    syncthing = None
    if syncthing_fn is not None:
        try:
            syncthing = syncthing_fn()
        except Exception:  # pragma: no cover
            syncthing = None

    return HealthOut(
        status="ok",
        state_db=HealthStateDb(
            path=str(db_path), tracks=stats["tracks"],
            tracks_playable=int(stats.get("tracks_playable", 0)),
            playlists=stats["playlists"], pairings=stats["pairings"],
            last_writer_hostname=last_writer[0] if last_writer else None,
            last_writer_at=last_writer[1] if last_writer else None,
        ),
        cloud=cloud,
        syncthing=(HealthSyncthing(**syncthing)
                   if isinstance(syncthing, dict) else None),
        waveform_materialization=HealthWaveformMaterialization(
            **rb_vendor.waveform_materialization_status()
        ),
        bind_host=bind_host,
        version=version,
        google_oauth_configured=GoogleOAuthConfig.is_configured(dict(os.environ)),
        # OPS-32 round 4: narrowed from every env key name (round 3) to just
        # the ones matching a forbidden prefix, plus one control bit -- round
        # 3's full key list was readable by an unauthenticated caller on a
        # token-mode share host, since /health is exempt from that gate so
        # cloudflared can probe it pre-auth (apps/webui/server/share_gate.py
        # EXEMPT_SUFFIXES). Names only, never values, and never the harmless
        # majority of the environment.
        process_env_forbidden_keys=sorted(
            key for key in os.environ if key.startswith(OPS32_FORBIDDEN_ENV_PREFIXES)
        ),
        process_env_home_present="HOME" in os.environ,
    )
