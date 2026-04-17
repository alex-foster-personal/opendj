"""Health endpoint (CAT-05)."""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, Request

from ..backend import StateBackend
from ..deps import get_read_state
from ..models import HealthCloud, HealthOut, HealthStateDb, HealthSyncthing

router = APIRouter(prefix="/health", tags=["health"])


@router.get("", response_model=HealthOut)
def health(
    request: Request,
    backend: StateBackend = Depends(get_read_state),
) -> HealthOut:
    stats = backend.stats()
    last_writer = backend.last_writer()
    db_path = getattr(request.app.state, "state_db_path", "data/state/state.db")
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
        bind_host=bind_host, version=version,
    )
