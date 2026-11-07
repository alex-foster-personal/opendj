"""FastAPI wiring helpers extracted from ``create_app``.

``create_app`` stays the public factory in :mod:`apps.webui.server.app`.
These helpers own CORS, middleware, router mounts, and lifespan so that
factory stays under the complexity and file-size ratchets.
"""
from __future__ import annotations

import importlib
import logging
import os
import pkgutil
import socket
import sqlite3
import threading
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from types import ModuleType
from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.types import Scope

from apps.feature_flags import FlagStore, load_flags
from apps.feature_flags.http import flags_router
from apps.play_analytics.http import router as play_analytics_router
from apps.set_notes.http import create_router as create_set_notes_router
from apps.sets.api import router as sets_router
from apps.sets.master_tap import MASTER_TAP_CHANNEL_STATE
from apps.shared.rekordbox_writeback import RekordboxWritebackDisabled
from apps.shared.shutdown_bound import StopStep, run_stop_steps, stop_window_deadline
from apps.shared.state.db import StateStoreBusyError
from apps.sync_hub import hosted_config as sync_hub_hosted_config
from apps.sync_hub import hub_wal_keeper
from apps.sync_hub.service import router as sync_hub_router
from apps.webui.port_config import (
    PortConfigError,
    resolve_backend_port,
    resolve_frontend_port,
)

from . import (
    analysis_autostart,
    backfill_drain,
    boot_grace,
    coverage_drain,
    grid_phase_precompute,
    library_jobs_autostart,
    lyric_index_autostart,
)
from .app_watchers import build_auto_analyze_watcher, build_lyric_index_watcher
from .backend import (
    BackendError,
    ConflictError,
    InMemoryBackend,
    NotFoundError,
    StateBackend,
)
from .cloudsync_scheduler import CloudSyncScheduler
from .deps import get_write_state
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
from .http_guards import HttpGuardMiddleware
from .playlist_add import AlreadyExistsError, BulkLimitError, SmartlistImmutableError
from .playlist_move import SliceNotContiguousError, TargetInsideSliceError
from . import routes as routes_package
from .routes import client_errors as client_errors_routes
from .routes import client_events as client_events_routes
from .routes import ingest_cli_procs
from .routes import performance_remote_speaker as performance_remote_speaker_routes
from .routes import playlist_write as playlist_write_routes
from .routes import sync_billing_proxy as sync_billing_proxy_routes
from .routes import telemetry as telemetry_routes
from .routes import usb_volumes_sim as usb_volumes_sim_routes
from .routes import vocals as vocals_routes
from .routes import voice_probe as voice_probe_routes
from .sets_master_tap import OrderBusMasterTap
from .share_gate import ShareConfig
from .stem_cache_enforcer import StemCacheEnforcer
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


# ----- lifespan helpers: get-or-build the workers retained on app.state -----
# Each worker is kept ON THE APP rather than rebuilt per lifespan, for the
# reason spelled out at the top of _lifespan_context.


def _retained_cloudsync_scheduler_if_armed(app: FastAPI) -> CloudSyncScheduler | None:
    if not app.state.cloudsync_scheduler_armed:
        return None
    scheduler: CloudSyncScheduler | None = getattr(app.state, "cloudsync_scheduler", None)
    if scheduler is None:
        scheduler = CloudSyncScheduler(app)
        app.state.cloudsync_scheduler = scheduler
    return scheduler


def _retained_stem_cache_enforcer_if_armed(app: FastAPI) -> StemCacheEnforcer | None:
    if not getattr(app.state, "stem_cache_enforcer_armed", False):
        return None
    enforcer: StemCacheEnforcer | None = getattr(app.state, "stem_cache_enforcer", None)
    if enforcer is None:
        enforcer = StemCacheEnforcer(app)
        app.state.stem_cache_enforcer = enforcer
    return enforcer


def _retained_library_jobs_watcher(app: FastAPI) -> library_jobs_autostart.LibraryJobsWatcher:
    jobs_watcher: library_jobs_autostart.LibraryJobsWatcher | None = getattr(
        app.state, "library_jobs_watcher", None
    )
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
    return jobs_watcher


def _start_coverage_drain_if_armed(app: FastAPI) -> None:
    # HEALTH-05: built only on an app the daemon entry point ARMED; the
    # user setting (default on) is read by the drain itself, every tick.
    if not getattr(app.state, "coverage_drain_armed", False):
        return
    if getattr(app.state, "coverage_drain", None) is None:
        app.state.coverage_drain = coverage_drain.build_for_app(app)
    app.state.coverage_drain.start()


def _start_ahead_analysis_if_armed(app: FastAPI) -> None:
    # NATIVE-21: built only on an app the daemon entry point ARMED.
    if not getattr(app.state, "ahead_analysis_armed", False):
        return
    if getattr(app.state, "ahead_analysis", None) is None:
        from . import ahead_analysis

        app.state.ahead_analysis = ahead_analysis.build_for_app(app)
    app.state.ahead_analysis.start()


def _start_grid_phase_precompute_if_armed(app: FastAPI) -> None:
    if not getattr(app.state, "grid_phase_precompute_armed", False):
        return
    if getattr(app.state, "grid_phase_precompute", None) is None:
        app.state.grid_phase_precompute = grid_phase_precompute.build_for_app(app)
    app.state.grid_phase_precompute.start()


def _settle_orphaned_drains(app: FastAPI) -> None:
    """Settle the drained batches an earlier engine left open (backfill_drain).

    Only an app wired to a queue database (``analysis_db_path``) has any. A
    failure is logged rather than raised: an unreadable queue must not stop
    the engine from starting, and the batches stay as they were.
    """
    db = getattr(app.state, "analysis_db_path", None)
    if db is None:
        return
    try:
        settled = backfill_drain.settle_orphaned_drains(Path(db))
    except Exception:
        log.exception("could not settle Re-analyze batches an earlier engine left open")
        return
    if settled:
        log.warning(
            "settled %d Re-analyze batch(es) an earlier engine left unfinished: %s",
            len(settled),
            ", ".join(settled),
        )
def _start_ingest_enrichment_if_armed(app: FastAPI) -> None:
    # ENRICH-06: the post-import coordinator runs where the analysis drain is armed.
    if not getattr(app.state, "ahead_analysis_armed", False):
        return
    from . import ingest_enrichment

    ingest_enrichment.coordinator_for(app).start()


@asynccontextmanager
async def _lifespan_context(app: FastAPI) -> AsyncIterator[None]:
    # A previous lifespan's stop_all closed the CLI registry; this app runs
    # refresh steps again.
    ingest_cli_procs.reopen()
    # Before anything can start a drain: a Re-analyze batch an earlier engine
    # never finished would otherwise stay queued with no runner forever.
    _settle_orphaned_drains(app)
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
    stem_cache_enforcer: StemCacheEnforcer | None = None
    try:
        lyric_watcher = getattr(app.state, "lyric_index_watcher", None)
        if lyric_watcher is None:
            lyric_watcher = build_lyric_index_watcher(app)
            app.state.lyric_index_watcher = lyric_watcher
        lyric_watcher.start()
        # FBSYNC-01: built only on an app the daemon entry point ARMED, and
        # even then inert unless MDT_CLOUDSYNC_SCHEDULER=1 and a hub URL are
        # set; retained on the app for the reason the watchers above are.
        cloudsync_scheduler = _retained_cloudsync_scheduler_if_armed(app)
        if cloudsync_scheduler is not None:
            cloudsync_scheduler.start()
        # STEM-39: armed with stem hydration. A tick evicts nothing unless a
        # hydration source is armed AND the disk is under its floor.
        stem_cache_enforcer = _retained_stem_cache_enforcer_if_armed(app)
        if stem_cache_enforcer is not None:
            stem_cache_enforcer.start()
            # OFFLOAD: the same daemon-only arming resumes interrupted offloads
            # and runs the re-offload sweep (spec e8da14b8 section 7).
            from .cloud_offload_service import service_for as _offload_service

            _offload_service(app).arm()
        _retained_library_jobs_watcher(app).start()
        _start_coverage_drain_if_armed(app)
        _start_ahead_analysis_if_armed(app)
        _start_grid_phase_precompute_if_armed(app)
        _start_ingest_enrichment_if_armed(app)
        from . import path_availability_refresh

        path_availability_refresh.start_for_state_db(
            Path(app.state.state_db_path), grace=boot_grace.for_app(app)
        )
        from . import state_maintenance

        state_maintenance.start_for_state_db(Path(app.state.state_db_path))
        yield
    finally:
        # A step still running would outlive the engine with its pool
        # workers. First, before the thread joins below (CloudSync waits up
        # to 30 s), so they cannot push it past the shell's grace.
        ingest_cli_procs.stop_all()
        from . import path_availability_refresh, state_maintenance

        # W43: all at once under one deadline, not one after another. Each
        # joins its thread (5 s for most, 30 s for CloudSync), so in series
        # a quit waited on their sum; these are daemon threads, and one
        # still busy at the deadline is logged and ends with the process.
        steps: list[StopStep] = [
            ("path-availability-refresh", path_availability_refresh.stop),
            ("state-maintenance", state_maintenance.stop),
        ]
        if cloudsync_scheduler is not None:
            steps.append(("cloudsync-scheduler", cloudsync_scheduler.stop))
        for name, attr in (
            ("coverage-drain", "coverage_drain"),
            ("ahead-analysis", "ahead_analysis"),
            ("grid-phase-precompute", "grid_phase_precompute"),
            ("ingest-enrichment", "ingest_enrichment"),
            ("library-jobs-watcher", "library_jobs_watcher"),
        ):
            service = getattr(app.state, attr, None)
            if service is not None:
                steps.append((name, service.stop))
        if stem_cache_enforcer is not None:
            steps.append(("stem-cache-enforcer", stem_cache_enforcer.stop))
        offload_service = getattr(app.state, "cloud_offload_service", None)
        if offload_service is not None:
            steps.append(("cloud-offload", offload_service.stop))
        quality_runner = getattr(app.state, "audio_quality_runner", None)
        if quality_runner is not None:
            steps.append(("audio-quality-scan", quality_runner.shutdown))
        if lyric_watcher is not None:
            steps.append(("lyric-index", lyric_watcher.stop))
        steps.append(("analysis-watcher", watcher.stop))
        # Inside the engine, this shares the window its lifespan opened (one
        # deadline for every nested group); standalone, it opens its own.
        run_stop_steps("webui lifespan", steps, deadline=stop_window_deadline(app.state))
        # playlist_write builds its PlaylistStore lazily from
        # app.state.state_db_path; release its sqlite handle on shutdown.
        playlist_write_routes.close_store(app)
        # I18: the remote speaker's LAN listener is the engine's to close, so
        # its port is released with the engine rather than left to task teardown.
        await performance_remote_speaker_routes.close_remote_speaker(app)
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
    from apps.cloud import stem_hydration
    from apps.cloud.stem_source import DirectR2Source

    app.state.stem_hydration_source = source
    app.state.stem_hydration_data_dir = data_dir
    # This process serves decks, so its registry is the one eviction may trust.
    stem_hydration.OPEN_DECKS.holds_decks = True
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
    # STEM-39: the disk-aware cache budget is read against this data dir by
    # the status route on every app, and enforced on a timer only on an app
    # the daemon entry point armed for hydration.
    app.state.stem_cache_data_dir = Path(data_dir)
    app.state.stem_cache_enforcer_armed = enabled
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
    # One pure-ASGI layer replaces five BaseHTTPMiddleware decorators (bind
    # warning, passive usage, share gate, origin guard, host allowlist, in
    # that outer-to-inner order) so response bodies are not re-streamed per
    # layer. See http_guards.py for the order contract.
    app.add_middleware(HttpGuardMiddleware, bind_host=bind_host)


def _routers_under(package: ModuleType) -> list[APIRouter]:
    """Routers of the modules in ``package`` that set ``MOUNT_UNDER_API_PREFIX``.

    Modules are imported in name order, so the mount order is stable and needs no
    hand list; a module without the flag is left alone (helpers, child modules
    that add to a parent's router, and the conditional routers below).
    """
    routers: list[APIRouter] = []
    for info in sorted(pkgutil.iter_modules(package.__path__), key=lambda i: i.name):
        module = importlib.import_module(f"{package.__name__}.{info.name}")
        if getattr(module, "MOUNT_UNDER_API_PREFIX", False) is not True:
            continue
        router = getattr(module, "router", None)
        if not isinstance(router, APIRouter):
            raise TypeError(
                f"{module.__name__} sets MOUNT_UNDER_API_PREFIX but has no "
                "module-level APIRouter named 'router'"
            )
        routers.append(router)
    return routers


def _mount_api_routers(app: FastAPI) -> None:
    api_prefix = "/api/v1"
    for router in (
        *_routers_under(routes_package),
        create_set_notes_router(get_write_state),
        flags_router,
    ):
        app.include_router(router, prefix=api_prefix)
    if app.state.usb_simulation_enabled:
        # Test/dev only (MDT_USB_SIMULATION=1): production apps never mount
        # the simulated-volume POST, so fake volumes cannot be injected.
        app.include_router(usb_volumes_sim_routes.router, prefix=api_prefix)
    if not getattr(app.state, sync_hub_hosted_config.HOSTED_FLAG_ATTR, False):
        app.include_router(sync_billing_proxy_routes.router, prefix=api_prefix)
    for router in (
        telemetry_routes.router,
        vocals_routes.router,
        voice_probe_routes.router,
        sync_hub_router,
    ):
        app.include_router(router, prefix=api_prefix)
    if not getattr(app.state, sync_hub_hosted_config.HOSTED_FLAG_ATTR, False):
        _drop_routes_shadowed_by(app, sync_billing_proxy_routes.router)
    app.include_router(sets_router)
    # SET-12: a master REC start pushes the attach to the open page.
    setattr(app.state, MASTER_TAP_CHANNEL_STATE, OrderBusMasterTap())
    app.include_router(play_analytics_router)


def _drop_routes_shadowed_by(app: FastAPI, shadowing: APIRouter) -> None:
    """Remove later routes that ``shadowing``'s routes already answer.

    Starlette dispatches to the first matching route, so the hub billing
    handlers behind the spoke proxy never run, but OpenAPI would still
    describe them (and their required ``machine_id`` body) for those paths.
    """
    ours = {route.endpoint for route in shadowing.routes if isinstance(route, APIRoute)}
    claimed: set[tuple[str, str]] = set()
    kept = []
    for route in app.router.routes:
        if isinstance(route, APIRoute):
            keys = {(route.path, method) for method in route.methods}
            if route.endpoint in ours:
                claimed |= keys
            elif keys & claimed:
                continue
        kept.append(route)
    app.router.routes[:] = kept


def _mount_frontend_or_placeholder(
    app: FastAPI, mount_frontend: bool, frontend_dir: Path
) -> None:
    # UIURL-01: resolve_ui_address reads this; False until a bundle is mounted.
    app.state.spa_mounted = False
    if mount_frontend and frontend_dir.exists() and any(frontend_dir.iterdir()):
        app.mount("/", _SpaStaticFiles(directory=str(frontend_dir), html=True),
                  name="spa")
        app.state.spa_mounted = True
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
