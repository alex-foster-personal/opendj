"""FastAPI application wiring.

The ``app`` symbol is what ``uvicorn apps.webui.server.app:app`` imports.
Tests construct a fresh app via :func:`create_app` so they can inject a
seeded backend without leaking global state.
"""
from __future__ import annotations

import logging
import os
import socket
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .backend import (BackendError, ConflictError, InMemoryBackend,
                      NotFoundError, StateBackend)
from .errors import (handle_backend_error, handle_conflict, handle_not_found)
from .routes import health as health_routes
from .routes import pairings as pairings_routes
from .routes import playlists as playlists_routes
from .routes import progress as progress_routes
from .routes import queues as queues_routes
from .routes import rb_assets as rb_assets_routes
from .routes import settings as settings_routes
from .routes import tracks as tracks_routes

log = logging.getLogger(__name__)

FRONTEND_BUILD_DIR: Path = (
    Path(__file__).resolve().parent.parent / "frontend" / "build"
)


def create_app(
    *,
    backend: Optional[StateBackend] = None,
    bind_host: str = "127.0.0.1",
    hostname: Optional[str] = None,
    lock_status_fn: Optional[Callable[[], Any]] = None,
    syncthing_status_fn: Optional[Callable[[], Any]] = None,
    state_db_path: str = "data/state/state.db",
    version: str = "0.1.0",
    enable_cors: bool = True,
    mount_frontend: bool = True,
) -> FastAPI:
    """Build a configured FastAPI app."""
    app = FastAPI(
        title="music-dj-tools webui",
        version=version,
        description=(
            "Local-first web UI for music-dj-tools. Binds to 127.0.0.1 by "
            "default (D5 / CAT-05b). Override via MUSIC_DJ_BIND_HOST."
        ),
    )
    app.state.backend = backend or InMemoryBackend()
    app.state.bind_host = bind_host
    app.state.hostname = hostname or socket.gethostname()
    app.state.lock_status_fn = lock_status_fn
    app.state.syncthing_status_fn = syncthing_status_fn
    app.state.state_db_path = state_db_path
    app.state.version = version

    app.add_exception_handler(NotFoundError, handle_not_found)
    app.add_exception_handler(ConflictError, handle_conflict)
    app.add_exception_handler(BackendError, handle_backend_error)

    if enable_cors:
        # NOTE: wildcard allow_methods/allow_headers is safe because
        # allow_origins is restricted to the SvelteKit dev server on
        # loopback. If you set MUSIC_DJ_BIND_HOST to expose the daemon
        # on LAN / Tailscale, tighten these to an explicit list:
        # allow_methods=["GET","POST","PATCH","DELETE"] and
        # allow_headers=["Content-Type","If-Match"]. The If-Match header
        # must remain allowed for optimistic-concurrency preflights.
        # See apps/webui/README.md -> "CORS policy" for rationale.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[
                "http://localhost:5173", "http://127.0.0.1:5173",
                # Isolated e2e verify stacks (loopback-only, see
                # .planning/rekordbox-parity/e2e*): frontend :5273/:5275
                # talks to daemons :8686/:8688 via VITE_API_BASE.
                "http://localhost:5273", "http://127.0.0.1:5273",
                "http://localhost:5275", "http://127.0.0.1:5275",
            ],
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["ETag", "X-Bind-Warning"],
        )

    @app.middleware("http")
    async def add_bind_warning(request: Request, call_next):
        response = await call_next(request)
        if bind_host and bind_host != "127.0.0.1" and bind_host != "localhost":
            response.headers["X-Bind-Warning"] = (
                f"server is bound to {bind_host}; "
                "do not expose without Tailscale"
            )
        return response

    api_prefix = "/api/v1"
    app.include_router(tracks_routes.router, prefix=api_prefix)
    app.include_router(playlists_routes.router, prefix=api_prefix)
    app.include_router(pairings_routes.router, prefix=api_prefix)
    app.include_router(queues_routes.router, prefix=api_prefix)
    app.include_router(rb_assets_routes.router, prefix=api_prefix)
    app.include_router(progress_routes.router, prefix=api_prefix)
    app.include_router(health_routes.router, prefix=api_prefix)
    app.include_router(settings_routes.router, prefix=api_prefix)

    if mount_frontend and FRONTEND_BUILD_DIR.exists() and any(FRONTEND_BUILD_DIR.iterdir()):
        app.mount("/", StaticFiles(directory=str(FRONTEND_BUILD_DIR), html=True),
                  name="spa")
    else:
        @app.get("/", include_in_schema=False)
        def _index_placeholder() -> dict[str, str]:
            return {
                "status": "ok",
                "message": (
                    "music-dj-tools webui API running. Build the frontend "
                    "(cd apps/webui/frontend && pnpm install && pnpm build) "
                    "to serve the SPA here."
                ),
                "api_docs": "/docs",
                "openapi": "/openapi.json",
            }

    return app


def _build_default_app() -> FastAPI:
    bind_host = os.environ.get("MUSIC_DJ_BIND_HOST", "127.0.0.1")
    hostname = os.environ.get("MUSIC_DJ_HOSTNAME")
    # Phase 5 wiring: prefer SqliteBackend when ``data/state/state.db`` exists,
    # else fall back to the in-memory backend (keeps dev + tests fast).
    backend: Optional[StateBackend] = None
    try:
        from .sqlite_backend import make_backend
        backend = make_backend()
    except Exception as exc:  # pragma: no cover - defensive
        log.warning(
            "failed to build SqliteBackend; using InMemoryBackend: %s", exc,
        )
        backend = None
    return create_app(
        backend=backend, bind_host=bind_host, hostname=hostname,
    )


app: FastAPI = _build_default_app()


__all__ = ["FRONTEND_BUILD_DIR", "app", "create_app"]
