"""FastAPI wiring helpers extracted from ``create_app``.

``create_app`` stays the public factory in :mod:`apps.webui.server.app`.
These helpers own CORS, middleware, router mounts, and lifespan so that
factory stays under the complexity and file-size ratchets.
"""
from __future__ import annotations

import logging
import os
import socket
import sqlite3
import threading
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.types import Scope

from apps.feature_flags import FlagStore, load_flags
from apps.play_analytics.api import router as play_analytics_router
from apps.sets.api import router as sets_router
from apps.shared.rekordbox_writeback import RekordboxWritebackDisabled
from apps.shared.state.db import StateStoreBusyError
from apps.sync_hub import hosted_config as sync_hub_hosted_config
from apps.sync_hub import hub_wal_keeper
from apps.sync_hub.service import router as sync_hub_router
from apps.webui.port_config import (
    PortConfigError,
    resolve_backend_port,
    resolve_frontend_port,
)

from . import analysis_autostart, coverage_drain, library_jobs_autostart, lyric_index_autostart
from .backend import (
    BackendError,
    ConflictError,
    InMemoryBackend,
    NotFoundError,
    StateBackend,
)
from .cloudsync_scheduler import CloudSyncScheduler
from .errors import (
    handle_already_exists,
    handle_backend_error,
    handle_bulk_limit,
    handle_conflict,
    handle_not_found,
    handle_rekordbox_writeback_disabled,
    handle_slice_not_contiguous,
    handle_smartlist_immutable,
    handle_sqlite_busy_operational_error,
    handle_state_store_busy,
    handle_target_inside_slice,
)
from .playlist_add import AlreadyExistsError, BulkLimitError, SmartlistImmutableError
from .playlist_move import SliceNotContiguousError, TargetInsideSliceError
from .request_guard import (
    host_allowlist_middleware,
    origin_guard_middleware,
)
from .routes import analysis as analysis_routes
from .routes import analysis_backfill as analysis_backfill_routes
from .routes import analysis_queue as analysis_queue_routes
from .routes import analysis_source as analysis_source_routes
from .routes import audio_output_health as audio_output_health_routes
from .routes import auth as auth_routes
from .routes import autolists as autolists_routes
from .routes import bench as bench_routes
from .routes import bulk_edit as bulk_edit_routes
from .routes import client_errors as client_errors_routes
from .routes import client_events as client_events_routes
from .routes import cloudsync as cloudsync_routes
from .routes import cloudsync_config as cloudsync_config_routes
from .routes import cloudsync_fleet as cloudsync_fleet_routes
from .routes import cloudsync_ops as cloudsync_ops_routes
from .routes import cloudsync_policy as cloudsync_policy_routes
from .routes import cloudsync_status as cloudsync_status_routes
from .routes import commands as commands_routes
from .routes import copilot as copilot_routes
from .routes import coverage_drain as coverage_drain_routes
from .routes import dedup_review as dedup_review_routes
from .routes import error_feed as error_feed_routes
from .routes import feedback as feedback_routes
from .routes import feedback_attachments as feedback_attachments_routes
from .routes import feedback_performance_marks as feedback_performance_marks_routes
from .routes import feedback_pins as feedback_pins_routes
from .routes import feedback_replies as feedback_replies_routes
from .routes import feedback_sync as feedback_sync_routes
from .routes import find_replace as find_replace_routes
from .routes import health as health_routes
from .routes import ingest as ingest_routes
from .routes import ingest_materialize as ingest_materialize_routes
from .routes import ingest_pending as ingest_pending_routes
from .routes import ingest_upload as ingest_upload_routes
from .routes import library as library_routes
from .routes import library_jobs as library_jobs_routes
from .routes import lifecycle as lifecycle_routes
from .routes import lyrics_search as lyrics_search_routes
from .routes import lyrics_words as lyrics_words_routes
from .routes import midi_maps as midi_maps_routes
from .routes import mytag as mytag_routes
from .routes import pairing_capture as pairing_capture_routes
from .routes import pairings as pairings_routes
from .routes import performance_headphones as performance_headphones_routes
from .routes import performance_telemetry as performance_telemetry_routes
from .routes import play_it as play_it_routes
from .routes import playlist_history as playlist_history_routes
from .routes import playlist_sets as playlist_sets_routes
from .routes import playlist_write as playlist_write_routes
from .routes import playlist_writeback as playlist_writeback_routes
from .routes import playlists as playlists_routes
from .routes import preflight as preflight_routes
from .routes import progress as progress_routes
from .routes import quality as quality_routes
from .routes import queues as queues_routes
from .routes import rb_assets as rb_assets_routes
from .routes import rb_djay_sync as rb_djay_sync_routes
from .routes import rb_hot_cues as rb_hot_cues_routes
from .routes import reconcile as reconcile_routes
from .routes import rekordbox_gate as rekordbox_gate_routes
from .routes import relocate as relocate_routes
from .routes import rescue_snapshots as rescue_snapshots_routes
from .routes import search as search_routes
from .routes import settings as settings_routes
from .routes import settings_ai as settings_ai_routes
from .routes import share as share_routes
from .routes import shell as shell_routes
from .routes import smartlists as smartlists_routes
from .routes import spotify as spotify_routes
from .routes import sql_playground as sql_playground_routes
from .routes import state as state_routes
from .routes import stem_tiers as stem_tiers_routes
from .routes import stems as stems_routes
from .routes import stems_assets as stems_assets_routes
from .routes import telemetry as telemetry_routes
from .routes import telemetry_consent as telemetry_consent_routes
from .routes import track_plays as track_plays_routes
from .routes import tracks as tracks_routes
from .routes import ui_prefs as ui_prefs_routes
from .routes import usb_export as usb_export_routes
from .routes import usb_volumes as usb_volumes_routes
from .routes import usb_volumes_sim as usb_volumes_sim_routes
from .routes import vocals as vocals_routes
from .routes import voice_probe as voice_probe_routes
from .routes import worktree_ports as worktree_ports_routes
from .share_gate import ShareConfig, share_gate_middleware
from .usage_telemetry import UsageStore

log = logging.getLogger(__name__)

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


def _resolve_ports(
    port: int | None, frontend_port: int | None
) -> tuple[int | None, int | None]:
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
    return port, frontend_port


def _start_coverage_drain(app: FastAPI) -> None:
    """HEALTH-05: built only on an app the daemon entry point ARMED; the
    user setting (default on) is read by the drain itself, every tick."""
    if not getattr(app.state, "coverage_drain_armed", False):
        return
    if getattr(app.state, "coverage_drain", None) is None:
        app.state.coverage_drain = coverage_drain.build_for_app(app)
    app.state.coverage_drain.start()


def _stop_coverage_drain(app: FastAPI) -> None:
    drain = getattr(app.state, "coverage_drain", None)
    if drain is not None:
        drain.stop()


@asynccontextmanager
async def _lifespan_context(app: FastAPI) -> AsyncIterator[None]:
    from .app import build_auto_analyze_watcher, build_lyric_index_watcher

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
    # build_lyric_index_watcher's own layout guard raises RuntimeError
    # when state_db_path is not data_dir/state/state.db, so its build
    # and start live INSIDE this try: a raise here must still stop the
    # auto-analyze watcher above via the finally, not leak its thread.
    lyric_watcher: lyric_index_autostart.LyricIndexWatcher | None = None
    cloudsync_scheduler: CloudSyncScheduler | None = None
    try:
        lyric_watcher = getattr(app.state, "lyric_index_watcher", None)
        if lyric_watcher is None:
            lyric_watcher = build_lyric_index_watcher(app)
            app.state.lyric_index_watcher = lyric_watcher
        lyric_watcher.start()
        # FBSYNC-01: built only on an app the daemon entry point ARMED, and
        # even then inert unless MDT_CLOUDSYNC_SCHEDULER=1 and a hub URL are
        # set; retained on the app for the reason the watchers above are.
        if app.state.cloudsync_scheduler_armed:
            cloudsync_scheduler = getattr(app.state, "cloudsync_scheduler", None)
            if cloudsync_scheduler is None:
                cloudsync_scheduler = CloudSyncScheduler(app)
                app.state.cloudsync_scheduler = cloudsync_scheduler
            cloudsync_scheduler.start()
        jobs_watcher = getattr(app.state, "library_jobs_watcher", None)
        if jobs_watcher is None:
            db = Path(app.state.state_db_path)
            data_dir = db.parent.parent if db.parent.name == "state" else db.parent
            roots = getattr(app.state, "stem_roots", None)
            stems_root = Path(roots[0]) if roots else data_dir / "state" / "stems"
            jobs_state = getattr(app.state, "auto_user_jobs", None)
            enabled = bool(jobs_state is not None and jobs_state.enabled)
            jobs_watcher = library_jobs_autostart.LibraryJobsWatcher(
                state_db=db,
                stems_root=stems_root,
                data_dir=data_dir,
                enabled=enabled,
            )
            app.state.library_jobs_watcher = jobs_watcher
        jobs_watcher.start()
        _start_coverage_drain(app)
        from . import path_availability_refresh

        path_availability_refresh.start_for_state_db(Path(app.state.state_db_path))
        yield
    finally:
        from . import path_availability_refresh

        path_availability_refresh.stop()
        if cloudsync_scheduler is not None:
            cloudsync_scheduler.stop()
        _stop_coverage_drain(app)
        jobs_w = getattr(app.state, "library_jobs_watcher", None)
        if jobs_w is not None:
            jobs_w.stop()
        if lyric_watcher is not None:
            lyric_watcher.stop()
        watcher.stop()
        # playlist_write builds its PlaylistStore lazily from
        # app.state.state_db_path; release its sqlite handle on shutdown.
        playlist_write_routes.close_store(app)
        # The hub's idle WAL keepers live on app.state for the app's life;
        # release them so a stopped app holds no handle on the hub database
        # (exclusive maintenance, Windows file replacement). LIBM-120 L6.
        hub_wal_keeper.close_wal_keepers(app.state)


def _bind_core_state(
    app: FastAPI,
    backend: StateBackend | None,
    bind_host: str,
    hostname: str | None,
    port: int | None,
    lock_status_fn: Callable[[], Any] | None,
    syncthing_status_fn: Callable[[], Any] | None,
    state_db_path: str,
) -> None:
    app.state.backend = backend or InMemoryBackend()
    app.state.bind_host = bind_host
    app.state.port = port
    app.state.hostname = hostname or socket.gethostname()
    app.state.lock_status_fn = lock_status_fn
    app.state.syncthing_status_fn = syncthing_status_fn
    app.state.state_db_path = state_db_path
    app.state.analysis_db_path = state_db_path
    app.state.usb_simulation_enabled = usb_volumes_sim_routes.simulation_enabled()
    # MDT_SYNC_HUB_HOSTED: off unless "1"; hosted mode fails fast right here.
    sync_hub_hosted_config.configure(app, os.environ, db_path=Path(state_db_path))


def _bind_feature_state(
    app: FastAPI,
    version: str,
    feature_flags: FlagStore | None,
    share_config: ShareConfig | None,
    auto_analyze: bool,
    lyric_index: bool,
    client_error_log_dir: Path | None,
    client_event_log_dir: Path | None,
) -> None:
    if feature_flags is not None:
        app.state.feature_flags = feature_flags
    else:
        from apps.shared.paths import DATA_DIR

        app.state.feature_flags = load_flags(DATA_DIR)
    app.state.version = version
    app.state.share_config = share_config or ShareConfig.from_environ()
    app.state.auto_analyze = analysis_autostart.build(enabled=auto_analyze)
    app.state.lyric_index = lyric_index_autostart.build(enabled=lyric_index)
    app.state.client_error_log_dir = (
        client_error_log_dir
        if client_error_log_dir is not None
        else client_errors_routes.DEFAULT_LOG_DIR
    )
    app.state.performance_log_dir = (
        client_error_log_dir
        if client_error_log_dir is not None
        else client_errors_routes.DEFAULT_LOG_DIR
    )
    app.state.client_event_log_dir = (
        client_event_log_dir
        if client_event_log_dir is not None
        else client_events_routes.DEFAULT_LOG_DIR
    )


def _bind_stem_and_usage(
    app: FastAPI,
    stem_roots: Sequence[Path] | None,
    usage_store: UsageStore | None,
) -> None:
    if stem_roots is not None:
        app.state.stem_roots = tuple(Path(root) for root in stem_roots)
    # Usage telemetry is per-process by design: "is the app open" is a
    # question about now, so a restart honestly resets it to "nobody has
    # checked in yet". Tests inject a store with a fake clock.
    app.state.usage_store = usage_store if usage_store is not None else UsageStore()


def _startup_stem_index_refresh(source, data_dir: Path) -> None:
    with suppress(Exception):
        source.refresh_index(Path(data_dir), force=True)


def _install_armed_stem_hydration(
    app: FastAPI,
    *,
    data_dir: Path,
    source,
    start_refresh_thread: bool = True,
) -> None:
    from apps.cloud.stem_source import DirectR2Source

    app.state.stem_hydration_source = source
    app.state.stem_hydration_data_dir = data_dir
    app.state.stem_hydration_unarmed_reason = None
    app.state.stem_hydration_unarmed_kind = None
    app.state.stem_hydration_cfg = None
    app.state.stem_hydration_s3 = None
    if isinstance(source, DirectR2Source):
        app.state.stem_hydration_cfg = source.cfg
        app.state.stem_hydration_s3 = source.s3
    if start_refresh_thread:
        threading.Thread(
            target=_startup_stem_index_refresh,
            args=(source, data_dir),
            name="opendj-stem-index-startup-refresh",
            daemon=True,
        ).start()


def _bind_stem_hydration(app: FastAPI, *, data_dir: Path, enabled: bool) -> None:
    """Wire on-demand stem hydration onto ``app.state`` (ADR-0024 / ADR-0051),
    or leave it unset.

    Unset is a legitimate machine state, not a failure: local mode, or a
    machine with neither R2 credentials nor a configured hub, simply never
    hydrates on demand. Routes check ``stem_hydration_source`` for ``None``
    and fall back to pre-hydration behavior.

    Configured but unable to arm (cloud mode with R2 credentials but no boto3,
    or CloudSync enabled but hub unreachable / no sync credential at boot) is
    NOT that legitimate state: the engine still boots, but
    ``stem_hydration_unarmed_reason`` is set and stems misses answer 502
    ``STEM_HYDRATION_NOT_ARMED``.
    """
    from apps.cloud.stem_source import arm_stem_hydration_source

    app.state.stem_hydration_source = None
    app.state.stem_hydration_data_dir = None
    app.state.stem_hydration_unarmed_reason = None
    app.state.stem_hydration_unarmed_kind = None
    # Legacy test injection points; production uses stem_hydration_source.
    app.state.stem_hydration_cfg = None
    app.state.stem_hydration_s3 = None
    if not enabled:
        return
    data_dir = Path(data_dir)
    armed = arm_stem_hydration_source(data_dir)
    if armed.unarmed_reason is not None:
        app.state.stem_hydration_unarmed_reason = armed.unarmed_reason
        app.state.stem_hydration_unarmed_kind = armed.unarmed_kind
        app.state.stem_hydration_data_dir = data_dir
        return
    if armed.source is None:
        return
    _install_armed_stem_hydration(app, data_dir=data_dir, source=armed.source)


def _install_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(NotFoundError, handle_not_found)
    app.add_exception_handler(
        RekordboxWritebackDisabled, handle_rekordbox_writeback_disabled
    )
    app.add_exception_handler(ConflictError, handle_conflict)
    app.add_exception_handler(AlreadyExistsError, handle_already_exists)
    app.add_exception_handler(SmartlistImmutableError, handle_smartlist_immutable)
    app.add_exception_handler(SliceNotContiguousError, handle_slice_not_contiguous)
    app.add_exception_handler(TargetInsideSliceError, handle_target_inside_slice)
    app.add_exception_handler(BulkLimitError, handle_bulk_limit)
    app.add_exception_handler(BackendError, handle_backend_error)
    app.add_exception_handler(StateStoreBusyError, handle_state_store_busy)
    app.add_exception_handler(
        sqlite3.OperationalError, handle_sqlite_busy_operational_error,
    )


def _configure_cors(app: FastAPI) -> None:
    # NOTE: wildcard allow_methods/allow_headers is safe because
    # allow_origins is restricted to the SvelteKit dev server on
    # loopback. If you set MUSIC_DJ_BIND_HOST to expose the daemon
    # on LAN / Tailscale, tighten these to an explicit list:
    # allow_methods=["GET","POST","PATCH","DELETE"] and
    # allow_headers=["Content-Type","If-Match"]. The If-Match header
    # must remain allowed for optimistic-concurrency preflights.
    # See apps/webui/README.md -> "CORS policy" for rationale.
    trusted_origins = getattr(app.state, "trusted_origins", ())
    trusted_origin_regex = getattr(app.state, "trusted_origin_regex", None)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(trusted_origins),
        allow_origin_regex=trusted_origin_regex,
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


def _configure_http_middleware(app: FastAPI, bind_host: str) -> None:
    app.middleware("http")(host_allowlist_middleware)
    app.middleware("http")(origin_guard_middleware)
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
        if bind_host and bind_host not in {"127.0.0.1", "localhost"}:
            response.headers["X-Bind-Warning"] = (
                f"server is bound to {bind_host}; do not expose without Tailscale"
            )
        return response


def _mount_api_routers(app: FastAPI) -> None:
    api_prefix = "/api/v1"
    prefixed = (
        tracks_routes.router,
        client_errors_routes.router,
        telemetry_consent_routes.router,
        error_feed_routes.router,
        client_events_routes.router,
        performance_telemetry_routes.router,
        performance_headphones_routes.router,
        rescue_snapshots_routes.router,
        bench_routes.router,
        bulk_edit_routes.router,
        find_replace_routes.router,
        mytag_routes.router,
        playlists_routes.router,
        playlist_write_routes.router,
        playlist_sets_routes.router,
        playlist_history_routes.router,
        play_it_routes.router,
        playlist_writeback_routes.router,
        pairings_routes.router,
        pairing_capture_routes.router,
        queues_routes.router,
        dedup_review_routes.router,
        feedback_routes.router,
        feedback_attachments_routes.router,
        feedback_performance_marks_routes.router,
        feedback_pins_routes.router,
        feedback_replies_routes.router,
        feedback_sync_routes.router,
        share_routes.router,
        rb_assets_routes.router,
        search_routes.router,
        rb_hot_cues_routes.router,
        track_plays_routes.router,
        progress_routes.router,
        quality_routes.router,
        worktree_ports_routes.router,
        sql_playground_routes.router,
        smartlists_routes.router,
        autolists_routes.router,
        stems_routes.router,
        stems_assets_routes.router,
        stem_tiers_routes.router,
        rb_djay_sync_routes.router,
        reconcile_routes.router,
        rekordbox_gate_routes.router,
        relocate_routes.router,
        copilot_routes.router,
        analysis_routes.router,
        audio_output_health_routes.router,
        analysis_backfill_routes.router,
        analysis_queue_routes.router,
        library_jobs_routes.router,
        coverage_drain_routes.router,
        analysis_source_routes.router,
        auth_routes.router,
        ingest_routes.router,
        ingest_upload_routes.router,
        ingest_materialize_routes.router,
        ingest_pending_routes.router,
        library_routes.router,
        lifecycle_routes.router,
        lyrics_search_routes.router,
        lyrics_words_routes.router,
        health_routes.router,
        preflight_routes.router,
        settings_routes.router,
        settings_ai_routes.router,
        midi_maps_routes.router,
        state_routes.router,
        commands_routes.router,
        shell_routes.router,
        ui_prefs_routes.router,
        cloudsync_routes.router,
        cloudsync_ops_routes.router,
        cloudsync_policy_routes.router,
        cloudsync_status_routes.router,
        cloudsync_config_routes.router,
        cloudsync_fleet_routes.router,
        spotify_routes.router,
        usb_export_routes.router,
        usb_volumes_routes.router,
    )
    for router in prefixed:
        app.include_router(router, prefix=api_prefix)
    if app.state.usb_simulation_enabled:
        # Test/dev only (MDT_USB_SIMULATION=1): production apps never mount
        # the simulated-volume POST, so fake volumes cannot be injected.
        app.include_router(usb_volumes_sim_routes.router, prefix=api_prefix)
    for router in (
        telemetry_routes.router,
        vocals_routes.router,
        voice_probe_routes.router,
        sync_hub_router,
    ):
        app.include_router(router, prefix=api_prefix)
    app.include_router(sets_router)
    app.include_router(play_analytics_router)


def _mount_frontend_or_placeholder(
    app: FastAPI, mount_frontend: bool, frontend_dir: Path
) -> None:
    if mount_frontend and frontend_dir.exists() and any(frontend_dir.iterdir()):
        app.mount("/", _SpaStaticFiles(directory=str(frontend_dir), html=True),
                  name="spa")
        return

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
