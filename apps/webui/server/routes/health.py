"""Health endpoint (CAT-05)."""
from __future__ import annotations

import os
from typing import Any, Callable, Optional

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
from ..state_paths import resolve_state_db_path

router = APIRouter(prefix="/health", tags=["health"])


@router.get("", response_model=HealthOut)
def health(
    request: Request,
    backend: StateBackend = Depends(get_read_state),
) -> HealthOut:
    stats = backend.stats()
    last_writer = backend.last_writer()
    # PREFLIGHT-02 (#2589): the same resolver GET /api/v1/preflight calls, so
    # the two endpoints cannot report two different files under one name.
    db_path = resolve_state_db_path(request)
    bind_host = getattr(request.app.state, "bind_host", "127.0.0.1")
    version = getattr(request.app.state, "version", "0.1.0")

    lock_fn: Optional[Callable[[], Any]] = getattr(
        request.app.state, "lock_status_fn", None
    )
    lock_holder = None
    if lock_fn is not None:
        try:
            lock_holder = lock_fn()
        except Exception:  # pragma: no cover
            lock_holder = None
    cloud = HealthCloud(lock_holder=lock_holder)

    syncthing_fn: Optional[Callable[[], Any]] = getattr(
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
        # OPS-32 round 3: names only, sorted for a stable diff. Never values --
        # this field exists so an external probe can verify the leak-scrub
        # without ever seeing what would have leaked.
        process_env_keys=sorted(os.environ.keys()),
    )
