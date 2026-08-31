"""FastAPI application wiring.

The ``app`` symbol is what ``uvicorn apps.webui.server.app:app`` imports.
Tests construct a fresh app via :func:`create_app` so they can inject a
seeded backend without leaking global state.
"""
from __future__ import annotations

import logging
import os
import socket
from collections.abc import Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.types import Scope

from apps.play_analytics.api import router as play_analytics_router
from apps.sets.api import router as sets_router
from apps.sync_hub.service import router as sync_hub_router
from apps.webui.port_config import (
    PortConfigError,
    resolve_backend_port,
    resolve_frontend_port,
)

from .backend import BackendError, ConflictError, InMemoryBackend, NotFoundError, StateBackend
from .cloud_sync import probe_syncthing_status
from .errors import handle_backend_error, handle_conflict, handle_not_found
from .routes import analysis as analysis_routes
from .routes import bench as bench_routes
from .routes import bulk_edit as bulk_edit_routes
from .routes import client_errors as client_errors_routes
from .routes import client_events as client_events_routes
from .routes import cloudsync as cloudsync_routes
from .routes import copilot as copilot_routes
from .routes import dedup_review as dedup_review_routes
from .routes import find_replace as find_replace_routes
from .routes import health as health_routes
from .routes import mytag as mytag_routes
from .routes import pairings as pairings_routes
from .routes import play_it as play_it_routes
from .routes import playlist_write as playlist_write_routes
from .routes import playlist_writeback as playlist_writeback_routes
from .routes import playlists as playlists_routes
from .routes import progress as progress_routes
from .routes import queues as queues_routes
from .routes import rb_assets as rb_assets_routes
from .routes import rb_hot_cues as rb_hot_cues_routes
from .routes import reconcile as reconcile_routes
from .routes import relocate as relocate_routes
from .routes import search as search_routes
from .routes import settings as settings_routes
from .routes import settings_ai as settings_ai_routes
from .routes import share as share_routes
from .routes import smartlists as smartlists_routes
from .routes import spotify as spotify_routes
from .routes import stem_tiers as stem_tiers_routes
from .routes import stems as stems_routes
from .routes import tracks as tracks_routes
from .routes import ui_prefs as ui_prefs_routes
from .routes import usb_export as usb_export_routes
from .routes import usb_volumes as usb_volumes_routes
from .routes import voice_probe as voice_probe_routes
from .share_gate import share_gate_middleware, share_host

log = logging.getLogger(__name__)

FRONTEND_BUILD_DIR: Path = (
    Path(__file__).resolve().parent.parent / "frontend" / "build"
)


class _SpaStaticFiles(StaticFiles):
    """Serve the SPA shell for extensionless client-side routes."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404 or not self._is_client_route(path):
                raise
            return await super().get_response("index.html", scope)

    @staticmethod
    def _is_client_route(path: str) -> bool:
        normalized_path = path.replace("\\", "/").lstrip("/")
        is_api_path = normalized_path == "api" or normalized_path.startswith("api/")
        return not is_api_path and not Path(normalized_path).suffix


def create_app(
    *,
    backend: Optional[StateBackend] = None,
    bind_host: str = "127.0.0.1",
    hostname: Optional[str] = None,
    lock_status_fn: Optional[Callable[[], Any]] = None,
    syncthing_status_fn: Optional[Callable[[], Any]] = None,
    state_db_path: str = "data/state/state.db",
    version: str = "0.1.0",
    port: Optional[int] = None,
    frontend_port: Optional[int] = None,
    enable_cors: bool = True,
    mount_frontend: bool = True,
    client_error_log_dir: Optional[Path] = None,
    client_event_log_dir: Optional[Path] = None,
    stem_roots: Optional[Sequence[Path]] = None,
) -> FastAPI:
    """Build a configured FastAPI app."""

    if port is None:
        try:
            port = resolve_backend_port(None)
        except PortConfigError:
            port = None
    if frontend_port is None:
        try:
            frontend_port = resolve_frontend_port()
        except PortConfigError:
            frontend_port = None

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        # playlist_write builds its PlaylistStore lazily from
        # app.state.state_db_path; release its sqlite handle on shutdown.
        playlist_write_routes.close_store(app)

    app = FastAPI(
        title="music-dj-tools webui",
        version=version,
        lifespan=_lifespan,
        description=(
            "Local-first web UI for music-dj-tools. Binds to 127.0.0.1 by "
            "default (D5 / CAT-05b). Override via MUSIC_DJ_BIND_HOST."
        ),
    )
    app.state.backend = backend or InMemoryBackend()
    app.state.bind_host = bind_host
    app.state.port = port
    app.state.hostname = hostname or socket.gethostname()
    app.state.lock_status_fn = lock_status_fn
    app.state.syncthing_status_fn = syncthing_status_fn
    app.state.state_db_path = state_db_path
    app.state.version = version
    app.state.client_error_log_dir = (
        client_error_log_dir
        if client_error_log_dir is not None
        else client_errors_routes.DEFAULT_LOG_DIR
    )
    app.state.client_event_log_dir = (
        client_event_log_dir
        if client_event_log_dir is not None
        else client_events_routes.DEFAULT_LOG_DIR
    )
    if stem_roots is not None:
        app.state.stem_roots = tuple(Path(root) for root in stem_roots)

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
        worktree_origins = (
            [
                f"http://localhost:{frontend_port}",
                f"http://127.0.0.1:{frontend_port}",
            ]
            if frontend_port is not None
            else []
        )
        share_origin = os.environ.get("MUSIC_DJ_SHARE_ORIGIN", "").strip()
        if not share_origin and share_host():
            share_origin = f"https://{share_host()}"
        share_origins = [share_origin] if share_origin else []
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[
                *worktree_origins,
                *share_origins,
                # Isolated e2e verify stacks (loopback-only, see
                # .planning/rekordbox-parity/e2e*): frontend :5273/:5275
                # talks to daemons :8686/:8688 via VITE_API_BASE.
                "http://localhost:5273", "http://127.0.0.1:5273",
                "http://localhost:5275", "http://127.0.0.1:5275",
            ],
            # scripts/bench/serve.py is a loopback static server for the
            # vocal quality rater; its port is a CLI arg (8791 by default,
            # 87xx in parallel runs), so it needs a pattern, not a literal.
            # POST /bench/ratings from that page is preflighted.
            allow_origin_regex=r"^http://(localhost|127\.0\.0\.1):87\d\d$",
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=[
                "ETag",
                "X-Bind-Warning",
                "X-Audio-Kind",
                "X-Audio-Venue",
                "X-Audio-Source",
            ],
        )

    app.middleware("http")(share_gate_middleware)

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
    app.include_router(client_errors_routes.router, prefix=api_prefix)
    app.include_router(client_events_routes.router, prefix=api_prefix)
    app.include_router(bench_routes.router, prefix=api_prefix)
    app.include_router(bulk_edit_routes.router, prefix=api_prefix)
    app.include_router(find_replace_routes.router, prefix=api_prefix)
    app.include_router(mytag_routes.router, prefix=api_prefix)
    app.include_router(playlists_routes.router, prefix=api_prefix)
    app.include_router(playlist_write_routes.router, prefix=api_prefix)
    app.include_router(play_it_routes.router, prefix=api_prefix)
    app.include_router(playlist_writeback_routes.router, prefix=api_prefix)
    app.include_router(pairings_routes.router, prefix=api_prefix)
    app.include_router(queues_routes.router, prefix=api_prefix)
    app.include_router(dedup_review_routes.router, prefix=api_prefix)
    app.include_router(share_routes.router, prefix=api_prefix)
    app.include_router(rb_assets_routes.router, prefix=api_prefix)
    app.include_router(search_routes.router, prefix=api_prefix)
    app.include_router(rb_hot_cues_routes.router, prefix=api_prefix)
    app.include_router(progress_routes.router, prefix=api_prefix)
    app.include_router(smartlists_routes.router, prefix=api_prefix)
    app.include_router(stems_routes.router, prefix=api_prefix)
    app.include_router(stem_tiers_routes.router, prefix=api_prefix)
    app.include_router(reconcile_routes.router, prefix=api_prefix)
    app.include_router(relocate_routes.router, prefix=api_prefix)
    app.include_router(copilot_routes.router, prefix=api_prefix)
    app.include_router(analysis_routes.router, prefix=api_prefix)
    app.include_router(health_routes.router, prefix=api_prefix)
    app.include_router(settings_routes.router, prefix=api_prefix)
    app.include_router(settings_ai_routes.router, prefix=api_prefix)
    app.include_router(ui_prefs_routes.router, prefix=api_prefix)
    app.include_router(cloudsync_routes.router, prefix=api_prefix)
    app.include_router(spotify_routes.router, prefix=api_prefix)
    app.include_router(usb_export_routes.router, prefix=api_prefix)
    app.include_router(usb_volumes_routes.router, prefix=api_prefix)
    app.include_router(voice_probe_routes.router, prefix=api_prefix)
    app.include_router(sync_hub_router, prefix=api_prefix)
    app.include_router(sets_router)
    app.include_router(play_analytics_router)

    if mount_frontend and FRONTEND_BUILD_DIR.exists() and any(FRONTEND_BUILD_DIR.iterdir()):
        app.mount("/", _SpaStaticFiles(directory=str(FRONTEND_BUILD_DIR), html=True),
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
    from apps.shared import platform_paths
    from apps.shared.library_mode import apply_library_env, assert_ready
    from apps.webui.library_assets import ensure_stem_storage, stem_storage

    apply_library_env()
    platform_paths.refresh_share_root()
    assert_ready()
    stems = stem_storage()
    ensure_stem_storage(stems)
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
        syncthing_status_fn=probe_syncthing_status,
        stem_roots=stems.roots,
    )


app: FastAPI = _build_default_app()


__all__ = ["FRONTEND_BUILD_DIR", "app", "create_app"]
