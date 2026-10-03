"""FastAPI dependencies for the webui daemon.

The read/write split follows the plan 11-02 step 2 design:

  * ``get_read_state`` -- any GET endpoint.
  * ``get_write_state`` -- any PATCH / POST / DELETE. If ``apps.cloud.lock``
    reports that another host is currently holding the writer lock, the
    dependency raises HTTP 503 with the holder info so the UI can surface
    the "another laptop is writing" state.

Backend selection:
  * tests override via ``app.dependency_overrides[get_backend] = ...``
  * production wires a real backend in :mod:`apps.webui.server.app` at
    startup (currently defaults to :class:`InMemoryBackend`; a
    sqlite-backed adapter over Phase 5's ``apps.shared.state`` is a
    planned follow-up -- see the Phase 5 status note in backend.py).
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import Depends, HTTPException, Request, status

from .backend import StateBackend
from .shell_output_health import data_dir_from_state_db


def get_backend(request: Request) -> StateBackend:
    backend: StateBackend | None = getattr(request.app.state, "backend", None)
    if backend is None:  # pragma: no cover - app always seeds one
        raise HTTPException(status_code=500, detail="state backend is not configured")
    return backend


_LOCK_PROBE_FAILED = "__lock_probe_failed__"


def get_lock_status(request: Request) -> dict[str, Any] | None:
    fn: Callable[[], Any] | None = getattr(request.app.state, "lock_status_fn", None)
    if fn is None:
        return None
    try:
        return fn()
    except Exception:
        # Fail-closed: if we cannot determine lock state, surface a sentinel
        # that ``get_write_state`` translates into a 503. Never silently fall
        # through to "no holder" — that would leave writes open during an
        # outage of the cloud-lock probe (codex P11-F01).
        return {"holder": _LOCK_PROBE_FAILED, "error": "lock_probe_failed"}


def get_library_data_dir(request: Request) -> Path:
    """This app's library data root, from ``state_db_path``: ``data_dir/state/state.db``
    or a flat ``data_dir/<name>.db`` (the same layout rule the shell-output health uses)."""
    return data_dir_from_state_db(Path(getattr(request.app.state, "state_db_path", "data/state/state.db")))


def get_read_state(backend: StateBackend = Depends(get_backend)) -> StateBackend:  # noqa: B008  # FastAPI DI
    return backend


def get_write_state(
    request: Request,
    backend: StateBackend = Depends(get_backend),  # noqa: B008  # FastAPI DI
    lock_status: dict[str, Any] | None = Depends(get_lock_status),  # noqa: B008  # FastAPI DI
) -> StateBackend:
    local_host = getattr(request.app.state, "hostname", "localhost")
    if lock_status and lock_status.get("holder") == _LOCK_PROBE_FAILED:
        # Probe raised — fail-closed (codex P11-F01).
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "lock_probe_failed",
                "message": (
                    "Cloud lock status is unavailable; writes are disabled "
                    "until the probe recovers."
                ),
                "holder": lock_status,
            },
        )
    if lock_status and lock_status.get("holder") and lock_status["holder"] != local_host:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "locked_by_peer",
                "message": (
                    f"Another host ({lock_status['holder']}) currently holds "
                    "the cloud lock; writes are disabled until it releases or "
                    "its TTL expires."
                ),
                "holder": lock_status,
            },
        )
    return backend


__all__ = ["get_backend", "get_lock_status", "get_read_state", "get_write_state"]
