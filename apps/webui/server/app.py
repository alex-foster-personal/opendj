"""FastAPI application wiring.

The ``app`` symbol is what ``uvicorn apps.webui.server.app:app`` imports.
It is built lazily on first attribute access (see ``__getattr__`` below), not
at module import time: ``_build_default_app()`` opens the real state.db and
runs ``apply_migrations`` on it (#762), and merely importing this module (for
``create_app``, as every webui test does) must never mutate a developer's or
CI's on-disk library as a side effect of collection.
Tests construct a fresh app via :func:`create_app` so they can inject a
seeded backend without leaking global state.
"""

from __future__ import annotations

import logging
import os
import socket
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.types import Scope

from apps.play_analytics.api import router as play_analytics_router
from apps.sets.api import router as sets_router
from apps.sets.share import SetShareConfig
from apps.shared.rekordbox_writeback import RekordboxWritebackDisabled
from apps.sync_hub.service import router as sync_hub_router
from apps.webui.port_config import (
    PortConfigError,
    resolve_backend_port,
    resolve_frontend_port,
)

from . import analysis_autostart
from .backend import BackendError, ConflictError, InMemoryBackend, NotFoundError, StateBackend
from .cloud_sync import probe_syncthing_status
from .errors import (
    handle_backend_error,
    handle_conflict,
    handle_not_found,
    handle_rekordbox_writeback_disabled,
)
from .routes import analysis as analysis_routes
from .routes import analysis_queue as analysis_queue_routes
from .routes import auth as auth_routes
from .routes import bench as bench_routes
from .routes import bulk_edit as bulk_edit_routes
from .routes import client_errors as client_errors_routes
from .routes import client_events as client_events_routes
from .routes import cloudsync as cloudsync_routes
from .routes import copilot as copilot_routes
from .routes import dedup_review as dedup_review_routes
from .routes import feedback as feedback_routes
from .routes import feedback_pins as feedback_pins_routes
from .routes import find_replace as find_replace_routes
from .routes import health as health_routes
from .routes import ingest as ingest_routes
from .routes import ingest_upload as ingest_upload_routes
from .routes import mytag as mytag_routes
from .routes import pairings as pairings_routes
from .routes import performance_telemetry as performance_telemetry_routes
from .routes import play_it as play_it_routes
from .routes import playlist_write as playlist_write_routes
from .routes import playlist_writeback as playlist_writeback_routes
from .routes import playlists as playlists_routes
from .routes import progress as progress_routes
from .routes import quality as quality_routes
from .routes import queues as queues_routes
from .routes import rb_assets as rb_assets_routes
from .routes import rb_hot_cues as rb_hot_cues_routes
from .routes import reconcile as reconcile_routes
from .routes import rekordbox_gate as rekordbox_gate_routes
from .routes import relocate as relocate_routes
from .routes import search as search_routes
from .routes import settings as settings_routes
from .routes import settings_ai as settings_ai_routes
from .routes import share as share_routes
from .routes import smartlists as smartlists_routes
from .routes import spotify as spotify_routes
from .routes import stem_tiers as stem_tiers_routes
from .routes import stems as stems_routes
from .routes import telemetry as telemetry_routes
from .routes import tracks as tracks_routes
from .routes import ui_prefs as ui_prefs_routes
from .routes import usb_export as usb_export_routes
from .routes import usb_volumes as usb_volumes_routes
from .routes import usb_volumes_sim as usb_volumes_sim_routes
from .routes import voice_probe as voice_probe_routes
from .share_gate import ShareConfig, share_gate_middleware
from .usage_telemetry import UsageStore

log = logging.getLogger(__name__)

FRONTEND_BUILD_DIR: Path = Path(__file__).resolve().parent.parent / "frontend" / "build"


class _SpaStaticFiles(StaticFiles):
    """Serve the SPA shell for extensionless client-side routes.

    Chrome (and WKWebView in the installed desktop app) heuristically caches
    index.html and even hashed chunks with no Cache-Control header, so a
    rebuilt app can keep serving a stale bundle. Vite content-hashes
    everything under _app/immutable/, so that path is safe to cache forever;
    the HTML entry point is never hashed, so it must always be revalidated.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404 or not self._is_client_route(path):
                raise
            response = await super().get_response("index.html", scope)
        return self._with_cache_control(path, response)

    @staticmethod
    def _with_cache_control(path: str, response: Response) -> Response:
        normalized_path = path.replace("\\", "/").lstrip("/")
        if normalized_path.startswith("_app/immutable/"):
            response.headers["cache-control"] = "public, max-age=31536000, immutable"
        elif getattr(response, "media_type", None) == "text/html":
            response.headers["cache-control"] = "no-cache"
        return response

    @staticmethod
    def _is_client_route(path: str) -> bool:
        normalized_path = path.replace("\\", "/").lstrip("/")
        is_api_path = normalized_path == "api" or normalized_path.startswith("api/")
        return not is_api_path and not Path(normalized_path).suffix


def create_app(
    *,
    backend: StateBackend | None = None,
    bind_host: str = "127.0.0.1",
    hostname: str | None = None,
    lock_status_fn: Callable[[], Any] | None = None,
    syncthing_status_fn: Callable[[], Any] | None = None,
    state_db_path: str = "data/state/state.db",
    version: str = "0.1.0",
    port: int | None = None,
    frontend_port: int | None = None,
    enable_cors: bool = True,
    mount_frontend: bool = True,
    client_error_log_dir: Path | None = None,
    client_event_log_dir: Path | None = None,
    stem_roots: Sequence[Path] | None = None,
    usage_store: UsageStore | None = None,
    share_config: ShareConfig | None = None,
    set_share_config: SetShareConfig | None = None,
    auto_analyze: bool = False,
) -> FastAPI:
    """Build a configured FastAPI app.

    ``auto_analyze`` arms the analyze-on-import reconcile loop (see
    :mod:`apps.webui.server.analysis_autostart`). It is OFF here on purpose:
    the loop shells out to ``apps.analysis.run``, so only the real daemon
    entry point (``_build_default_app``) turns it on, from
    ``MUSIC_DJ_AUTO_ANALYZE``. Tests opt in explicitly.
    """

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
        # Kept ON THE APP, not rebuilt per lifespan. stop() joins with a
        # timeout, so a shutdown that gives up on a slow scan leaves a live
        # thread whose only handle is the watcher that owns it. A fresh
        # watcher on the next start of the SAME app would know nothing about
        # that thread and would start a second reconcile loop beside it,
        # which is the one thing the retained handle exists to prevent.
        watcher = getattr(app.state, "auto_analyze_watcher", None)
        if watcher is None:
            watcher = build_auto_analyze_watcher(app)
            app.state.auto_analyze_watcher = watcher
        watcher.start()
        try:
            yield
        finally:
            watcher.stop()
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
    app.state.usb_simulation_enabled = usb_volumes_sim_routes.simulation_enabled()
    app.state.share_config = share_config or ShareConfig.from_environ()
    app.state.set_share_config = set_share_config or SetShareConfig.from_environ()
    app.state.auto_analyze = analysis_autostart.build(enabled=auto_analyze)
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
    # Usage telemetry is per-process by design: "is the app open" is a
    # question about now, so a restart honestly resets it to "nobody has
    # checked in yet". Tests inject a store with a fake clock.
    app.state.usage_store = usage_store if usage_store is not None else UsageStore()

    app.add_exception_handler(NotFoundError, handle_not_found)
    app.add_exception_handler(RekordboxWritebackDisabled, handle_rekordbox_writeback_disabled)
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
        if not share_origin and app.state.share_config.host:
            share_origin = f"https://{app.state.share_config.host}"
        share_origins = [share_origin] if share_origin else []
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[
                *worktree_origins,
                *share_origins,
                # Isolated e2e verify stacks (loopback-only, see
                # .planning/rekordbox-parity/e2e*): frontend :5273/:5275
                # talks to daemons :8686/:8688 via VITE_API_BASE.
                "http://localhost:5273",
                "http://127.0.0.1:5273",
                "http://localhost:5275",
                "http://127.0.0.1:5275",
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
    async def record_passive_usage(request: Request, call_next):
        # Backstop for the heartbeat: ordinary traffic from a real webview or
        # browser is evidence someone has the app open. The store drops
        # telemetry/health paths and non-app user agents, so agents polling
        # this API cannot manufacture the activity they are asking about.
        app.state.usage_store.record_request(
            path=request.url.path,
            user_agent=request.headers.get("user-agent", ""),
        )
        return await call_next(request)

    @app.middleware("http")
    async def add_bind_warning(request: Request, call_next):
        response = await call_next(request)
        if bind_host and bind_host != "127.0.0.1" and bind_host != "localhost":
            response.headers["X-Bind-Warning"] = (
                f"server is bound to {bind_host}; do not expose without Tailscale"
            )
        return response

    api_prefix = "/api/v1"
    app.include_router(tracks_routes.router, prefix=api_prefix)
    app.include_router(client_errors_routes.router, prefix=api_prefix)
    app.include_router(client_events_routes.router, prefix=api_prefix)
    app.include_router(performance_telemetry_routes.router, prefix=api_prefix)
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
    app.include_router(feedback_routes.router, prefix=api_prefix)
    app.include_router(feedback_pins_routes.router, prefix=api_prefix)
    app.include_router(share_routes.router, prefix=api_prefix)
    app.include_router(rb_assets_routes.router, prefix=api_prefix)
    app.include_router(search_routes.router, prefix=api_prefix)
    app.include_router(rb_hot_cues_routes.router, prefix=api_prefix)
    app.include_router(progress_routes.router, prefix=api_prefix)
    app.include_router(quality_routes.router, prefix=api_prefix)
    app.include_router(smartlists_routes.router, prefix=api_prefix)
    app.include_router(stems_routes.router, prefix=api_prefix)
    app.include_router(stem_tiers_routes.router, prefix=api_prefix)
    app.include_router(reconcile_routes.router, prefix=api_prefix)
    app.include_router(rekordbox_gate_routes.router, prefix=api_prefix)
    app.include_router(relocate_routes.router, prefix=api_prefix)
    app.include_router(copilot_routes.router, prefix=api_prefix)
    app.include_router(analysis_routes.router, prefix=api_prefix)
    app.include_router(analysis_queue_routes.router, prefix=api_prefix)
    app.include_router(auth_routes.router, prefix=api_prefix)
    app.include_router(ingest_routes.router, prefix=api_prefix)
    app.include_router(ingest_upload_routes.router, prefix=api_prefix)
    app.include_router(health_routes.router, prefix=api_prefix)
    app.include_router(settings_routes.router, prefix=api_prefix)
    app.include_router(settings_ai_routes.router, prefix=api_prefix)
    app.include_router(ui_prefs_routes.router, prefix=api_prefix)
    app.include_router(cloudsync_routes.router, prefix=api_prefix)
    app.include_router(spotify_routes.router, prefix=api_prefix)
    app.include_router(usb_export_routes.router, prefix=api_prefix)
    app.include_router(usb_volumes_routes.router, prefix=api_prefix)
    if app.state.usb_simulation_enabled:
        # Test/dev only (MDT_USB_SIMULATION=1): production apps never mount
        # the simulated-volume POST, so fake volumes cannot be injected.
        app.include_router(usb_volumes_sim_routes.router, prefix=api_prefix)
    app.include_router(telemetry_routes.router, prefix=api_prefix)
    app.include_router(voice_probe_routes.router, prefix=api_prefix)
    app.include_router(sync_hub_router, prefix=api_prefix)
    app.include_router(sets_router)
    app.include_router(play_analytics_router)

    if mount_frontend and FRONTEND_BUILD_DIR.exists() and any(FRONTEND_BUILD_DIR.iterdir()):
        app.mount("/", _SpaStaticFiles(directory=str(FRONTEND_BUILD_DIR), html=True), name="spa")
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


def create_process_app() -> FastAPI:
    """Uvicorn factory that names reload workers before they accept traffic.

    A reload worker re-imports this module fresh in a new process, so the
    lazy ``app`` singleton has not been built yet here. Call ``__getattr__``
    directly rather than reading the bare name ``app``: LOAD_GLOBAL resolves
    bare names from module globals without going through PEP 562, so it would
    raise ``NameError`` instead of building the app.
    """
    from apps.shared.process_identity import set_process_identity
    from apps.webui.port_config import resolve_backend_port

    set_process_identity("Backend", resolve_backend_port(None))
    return __getattr__("app")


def build_auto_analyze_watcher(app: FastAPI) -> analysis_autostart.AutoAnalyzeWatcher:
    """Bind the reconcile loop to the one-slot ingest refresh job.

    Public because it IS the wiring under test: a test that re-declares these
    three callables would keep passing after the daemon started draining the
    wrong scope. Call this instead.
    """

    def start_unmapped_drain(
        guard: analysis_autostart.GuardFn,
    ) -> analysis_autostart.ConsumedFn:
        """Start the drain and hand back a reader bound to THAT job.

        ``guard`` is the watcher's own refusal, and it is handed DOWN to the
        locked start rather than run here, so the ledger read it judges and
        the slot claim happen inside one hold of the registry lock. Run here
        instead and there is a moment between them again - short, but a
        parked thread is parked for as long as the scheduler says, and a
        drain that finishes in it makes this start a repeat of one that
        already attempted the same queue.

        The job object comes back FROM the locked start, so it is the one
        the registry just created and the reader survives a manual refresh
        claiming the single slot. Reread off the registry instead and the
        window is one statement wide - a short drain can finish and a library
        refresh claim the slot in it - and a library job, which publishes no
        queue signature, would then erase the booking.

        The signature is reached through the job rather than
        RefreshStatusOut because it is internal reconcile state, not part of
        the wire contract the browser polls: putting it on the response model
        would drift the OpenAPI schema for a value no client can use.
        """
        started = ingest_routes._start_refresh_job(
            ingest_routes.RefreshIn(scope="unmapped"),
            lambda: guard(last_attempted_queue()),
        )

        def consumed() -> str | None:
            # The LATEST FINISHED unmapped drain, which is not necessarily
            # ours: a manual POST /analysis-queue/run targets the same derived
            # queue, and the queue IT just failed on is the one worth
            # suppressing.
            #
            # Read off `last_unmapped` rather than the one slot, because the
            # slot is replaceable and a library or batch refresh landing after
            # that drain hides it completely. Its signature would then be read
            # as a silence and OUR older one booked instead - and when the
            # newer drain had deliberately CLEARED its signature, because the
            # CLI reported a target it never admitted, that older booking is
            # exactly the suppression the clearing existed to prevent. The
            # lost target then sits behind `unchanged` for as long as it keeps
            # its content token.
            latest = ingest_routes._JOBS.last_unmapped
            if latest is not None and latest.phase not in ingest_routes.ACTIVE_PHASES:
                return latest.queue_signature
            # No unmapped drain has finished in this process, or the newest
            # one is still running and its signature is not final. Fall back
            # to what OUR drain read rather than booking None, which would
            # drop the suppression entirely.
            return started.queue_signature

        return consumed

    def last_attempted_queue() -> analysis_autostart.Attempt | None:
        """The newest FINISHED drain, whoever started it.

        The watcher only ever hears about its OWN drains through
        ``start_unmapped_drain``. A manual POST /analysis-queue/run targets
        the same derived queue and is invisible to it, which is the hole this
        closes.

        The job object goes back with the signature so the watcher can tell a
        NEW attempt from the same finished job still sitting in the one-slot
        registry, and the SCOPE goes with it because the watcher cannot read
        a `None` signature without knowing whether the job was ever about this
        backlog. Identity and scope are the caller's business; only the
        signature is interpreted here.

        FINISHED is load-bearing and is checked, not assumed. A manual drain
        that claims the slot after ``running_fn`` said no publishes its
        signature EARLY - ``_unmapped_targets`` writes it before the first
        chunk runs - so a still-running job is visible here with a signature
        that is not its final one. Adopting it would book a queue whose verdict
        is not in yet, and the case that costs is the one the publish protocol
        exists for: a CLI that then reports a vanished target clears
        ``queue_signature`` precisely so the queue is NOT booked, and the
        booking would already have happened. Restoring that file recreates the
        same signature, the loop reports ``unchanged`` forever, and a track
        nothing ever attempted is never analyzed.
        """
        current = ingest_routes._JOBS.current
        if current is not None and current.scope == ingest_routes.UNMAPPED_SCOPE:
            if current.phase in ingest_routes.ACTIVE_PHASES:
                return None
            return analysis_autostart.Attempt(
                job=current,
                signature=current.queue_signature,
                targets_backlog=True,
            )
        # The slot holds a library or batch job, or nothing at all, and so
        # says nothing about this backlog. A finished unmapped drain it
        # REPLACED still does, and that statement has to outlive the slot for
        # the same reason `consumed` reads it: a drain that cleared its
        # signature is asking for its queue to be retried, and a library
        # refresh landing after it must not be what silences that.
        latest = ingest_routes._JOBS.last_unmapped
        if latest is not None and latest.phase not in ingest_routes.ACTIVE_PHASES:
            return analysis_autostart.Attempt(
                job=latest,
                signature=latest.queue_signature,
                targets_backlog=True,
            )
        if current is None or current.phase in ingest_routes.ACTIVE_PHASES:
            return None
        return analysis_autostart.Attempt(
            job=current,
            signature=current.queue_signature,
            targets_backlog=False,
        )

    return analysis_autostart.AutoAnalyzeWatcher(
        app.state.auto_analyze,
        scan_fn=ingest_routes.unmapped_backlog,
        start_fn=start_unmapped_drain,
        running_fn=lambda: ingest_routes.refresh_status().running,
        attempted_fn=last_attempted_queue,
    )


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
    # ``make_backend()`` itself already returns InMemoryBackend when no db
    # file exists; it only raises for a real construction/migration failure,
    # and that must abort this boot path too (#762) rather than silently
    # swallow into an empty in-memory library.
    from .sqlite_backend import make_backend

    backend: StateBackend = make_backend()
    return create_app(
        backend=backend,
        bind_host=bind_host,
        hostname=hostname,
        syncthing_status_fn=probe_syncthing_status,
        stem_roots=stems.roots,
        auto_analyze=analysis_autostart.arm_from_environ(os.environ),
    )


# Single-item cache mutated in place (not rebound) so ``__getattr__`` below
# needs no ``global`` statement.
_default_app_cache: list[FastAPI] = []


def __getattr__(name: str) -> Any:
    """Lazily build the module-scope ``app`` on first access (PEP 562).

    Deferring past plain import time means ``from .app import create_app``
    (every webui test) never triggers ``_build_default_app()``'s state.db
    migration side effect; only an actual attempt to serve (uvicorn's
    ``apps.webui.server.app:app`` lookup, or ``__main__.py``'s
    ``from apps.webui.server.app import app``) does.
    """
    if name == "app":
        if not _default_app_cache:
            _default_app_cache.append(_build_default_app())
        return _default_app_cache[0]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["FRONTEND_BUILD_DIR", "app", "create_app", "create_process_app"]  # noqa: F822 -- "app" is a PEP 562 lazy attribute, not a real binding
