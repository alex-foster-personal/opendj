"""``create_app(cfg)`` -- the engine chassis around the legacy routers.

The legacy FastAPI composition is REUSED, not reimplemented: this module
calls ``apps.webui.server.app.create_app`` and then applies three
chassis-level edits.

1. The legacy progress router stays mounted. Read and write for the fan-out
   ledger share ``apps/webui/server/routes/progress.py``.
2. ``/api/v1/health`` is replaced by a wrapper that calls the legacy handler
   and adds contract_rev / engine_version / boot_id. The legacy route
   function is untouched -- only the response is extended.
3. Jobs endpoints and the ``/api/v1/events`` socket are added, then the SPA
   is mounted LAST, because a Mount at "/" matches every path and would
   shadow anything registered after it.

IMPORTANT: importing this module imports ``apps.webui.server.app``, which
resolves its paths (and builds a default app) at import time. ``MDT_DATA_DIR``
must therefore already be set -- see ``config.apply_env_contract``, which
``serve`` calls before it imports this module.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.routing import APIRoute

from apps.cloud import job as cloud_job
from apps.engine_core.account.api import (
    account_router,
    entitlements_router,
    flags_router,
)
from apps.engine_core.app_posture_api import add_app_posture_route
from apps.engine_core.assistant.api import router as assistant_router
from apps.engine_core.audio_engine import AudioEngineSupervisor, autostart_from_environ
from apps.engine_core.audio_engine_api import add_audio_engine_routes
from apps.engine_core.audio_interference_api import add_audio_interference_route
from apps.engine_core.availability_api import add_availability_routes
from apps.engine_core.build_info import BUILD_IDENTITY_STATE_ATTR, add_build_info_route
from apps.engine_core.config import (
    ENGINE_VERSION,
    EngineBootError,
    EngineConfig,
    prepare_layout,
)
from apps.engine_core.contract import compute_contract_rev
from apps.engine_core.host_info import add_host_info_route
from apps.engine_core.jobs.api import router as jobs_router
from apps.engine_core.jobs.runner import (
    JobRunner,
    register_progress_observer,
    register_reconcile,
    register_worker,
)
from apps.engine_core.jobs.store import JobStore
from apps.engine_core.library_availability import (
    LibraryAvailabilityWorker,
    attach_library_changed_probe,
)
from apps.engine_core.lock import EngineLock
from apps.engine_core.perf_tier_api import add_perf_tier_route
from apps.engine_core.rescue_api import add_rescue_routes
from apps.engine_core.setup.api import router as setup_router
from apps.engine_core.setup.jobs import SETUP_IMPORT_KIND
from apps.engine_core.setup.library_events import on_setup_import_progress
from apps.engine_core.setup.folder_rescan_scheduler import folder_rescan_lifespan
from apps.engine_core.update_channel import add_update_apply_route, add_update_check_route
from apps.engine_core.ws import TOPIC_HEALTH_CHANGED, WsHub, events_endpoint
from apps.feature_flags import load_flags
from apps.shared import events, platform_paths
from apps.shared.library_mode import apply_library_env, assert_ready
from apps.shared.state import db as state_db
from apps.shared.sync_bind_guard import assert_sync_bind_allowed
from apps.stems import job as stems_job
from apps.stems.api import router as stems_plan_router
from apps.stems.live_capability import assess_install_once
from apps.stems.live_capability_api import router as live_stems_capability_router
from apps.sync_hub import first_run as cloudsync_first_run
from apps.sync_hub.scheduler import scheduler_lifespan
from apps.webui.library_assets import ensure_stem_storage, stem_storage
from apps.webui.server import analysis_autostart, library_jobs_autostart
from apps.webui.server.app import _SpaStaticFiles
from apps.webui.server.app import create_app as legacy_create_app
from apps.webui.server.backend import StateBackend
from apps.webui.server.cloud_sync import probe_syncthing_status
from apps.webui.server.deps import get_read_state
from apps.webui.server.frontend_build import frontend_build_dir
from apps.webui.server.models import HealthOut
from apps.webui.server.request_guard import assert_request_guard_bind_allowed
from apps.webui.server.routes.health import health as legacy_health
from apps.webui.server.sqlite_backend import make_backend

log = logging.getLogger(__name__)

API_PREFIX: str = "/api/v1"
PROGRESS_PREFIX: str = f"{API_PREFIX}/progress"
HEALTH_PATH: str = f"{API_PREFIX}/health"
EVENTS_PATH: str = f"{API_PREFIX}/events"

# 4x margin under lock.HEARTBEAT_TTL_S, so a single slow tick never makes a
# live engine look abandoned.
HEARTBEAT_INTERVAL_S: float = 30.0


class EngineHealthOut(HealthOut):
    """Legacy health plus the handshake fields. Extension, not a rewrite."""

    contract_rev: str
    engine_version: str
    boot_id: str


def create_app(
    cfg: EngineConfig, *, lock: EngineLock | None = None
) -> FastAPI:
    """Build the engine app. No import-time construction, no globals."""
    assert_sync_bind_allowed(cfg.host)
    assert_request_guard_bind_allowed(cfg.host)
    prepare_layout(cfg)

    boot_id = lock.boot_id if lock is not None else str(uuid.uuid4())
    app = _compose_legacy(cfg)

    _drop_or_raise(app, HEALTH_PATH, "legacy health route")
    _add_health_route(app)

    store = JobStore(cfg.jobs_db, boot_id=boot_id, owner_pid=os.getpid())
    recovered = store.recover()
    if recovered:
        log.warning(
            "engine boot %s recovered %d job(s) left by a previous engine: %s",
            boot_id,
            len(recovered),
            [job["id"] for job in recovered],
        )
    runner = JobRunner(store)
    # The chassis ships zero kinds; each kind opts in here. Registration is
    # process-global and idempotent, and it must happen before the jobs router
    # can be reached, or an enqueue for a real kind would 400 as unknown.
    _register_job_kinds()
    app.include_router(jobs_router, prefix=API_PREFIX)
    # Engine-only: the plan describes a run only an engine can start.
    app.include_router(stems_plan_router, prefix=API_PREFIX)
    app.include_router(live_stems_capability_router, prefix=API_PREFIX)
    # Importing the setup router is also what REGISTERS its job kind, so the
    # order matters: the jobs surface must be able to build a worker for
    # setup.import-rekordbox before anything can enqueue one.
    app.include_router(setup_router, prefix=API_PREFIX)
    # The sidebar assistant that keeps the user company while that import
    # runs. Stateless and dependency-free -- it reads its key and model off
    # the environment, so it needs nothing from app.state.
    app.include_router(assistant_router, prefix=API_PREFIX)
    # The commercial seam: account management, the entitlement resolver, and
    # the feature-flag file. All three ship inert -- there are no paid
    # features and no declared flags -- but they are registered here so the
    # committed OpenAPI contract carries them and an agent can drive the same
    # surfaces the account panel does.
    app.include_router(entitlements_router, prefix=API_PREFIX)
    app.include_router(flags_router, prefix=API_PREFIX)
    app.include_router(account_router, prefix=API_PREFIX)
    app.add_api_websocket_route(EVENTS_PATH, events_endpoint, name="events")
    # Identity before the SPA mount, like every other route: a Mount at "/"
    # swallows anything registered after it. platform_paths.PROJECT_ROOT is
    # the checkout root in a dev boot and the payload's app/ dir in a bundled
    # one, which is exactly the distinction the resolver switches on.
    add_build_info_route(
        app, environ=dict(os.environ), repo_root=platform_paths.PROJECT_ROOT
    )
    add_host_info_route(app, data_dir=cfg.data_dir)
    add_perf_tier_route(app, data_dir=cfg.data_dir)
    add_app_posture_route(app, data_dir=cfg.data_dir)
    add_audio_interference_route(app)
    build_identity = getattr(app.state, BUILD_IDENTITY_STATE_ATTR)
    if build_identity.info is not None and build_identity.info.source == "payload":
        # An installed engine owns the first-run assessment. A checkout has no
        # record because its development host is not the user's installed app.
        app.state.live_stems_capability = assess_install_once(cfg.data_dir)
    # The update channel, mounted AFTER build-info because it reads the
    # identity that route resolves onto app.state. It reports only: applying
    # an update is the desktop shell's Tauri updater, which owns signature
    # verification. Here so an agent and a browser tab can ask the same
    # question the shell's button asks.
    add_update_check_route(app)
    add_update_apply_route(app)
    availability_worker = LibraryAvailabilityWorker(cfg.data_dir)
    add_availability_routes(app, availability_worker)
    # Plan 20-02: this engine supervises odj-audio (the shell only bundles it
    # and sets ODJ_AUDIO_BIN). ODJ_AUDIO_ENGINE is read here so a bad value
    # stops the boot rather than surfacing on the first request.
    try:
        autostart_from_environ(os.environ)
    except ValueError as exc:
        raise EngineBootError(str(exc)) from exc
    audio_engine = AudioEngineSupervisor(
        environ=dict(os.environ), repo_root=platform_paths.PROJECT_ROOT
    )
    add_audio_engine_routes(app, audio_engine)
    app.state.engine_cfg = cfg
    add_rescue_routes(app)

    _drop_root_placeholder(app)
    _mount_spa(app)

    contract_rev = compute_contract_rev(app.openapi())
    hub = WsHub(contract_rev=contract_rev, engine_version=ENGINE_VERSION)
    app.state.engine_boot_id = boot_id
    app.state.contract_rev = contract_rev
    app.state.jobs_store = store
    app.state.jobs_runner = runner
    app.state.event_hub = hub
    # FLAG-03: read at STARTUP, from a local file, with no network. Resolving
    # per request would turn a deploy-time decision into a runtime one and let
    # a half-written file change behaviour mid-request. A malformed file
    # raises here and stops the boot rather than degrading into defaults.
    app.state.feature_flags = load_flags(cfg.data_dir)
    app.state.availability_worker = availability_worker

    _wrap_lifespan(
        app,
        cfg=cfg,
        hub=hub,
        store=store,
        runner=runner,
        lock=lock,
        availability_worker=availability_worker,
        audio_engine=audio_engine,
    )
    return app


# ----- job kinds ---------------------------------------------------------
def _register_job_kinds() -> None:
    """Wire every domain's job kind into the chassis registry.

    THE COMPOSITION ROOT, and the only place that knows both halves. The
    chassis ships zero kinds and a domain package must not import the chassis
    (that is a package cycle the architecture gate fails on), so the wiring
    lands here, where the engine already depends on both by definition.

    Idempotent: the registries are plain dicts keyed by kind, so a second
    create_app in the same process (every test module that builds an app)
    rebinds the same functions rather than accumulating them.
    """
    register_worker(stems_job.JOB_KIND, stems_job.build_argv)
    register_progress_observer(stems_job.JOB_KIND, stems_job.on_progress)
    register_reconcile(stems_job.JOB_KIND, stems_job.reconcile_from_disk)
    register_progress_observer(SETUP_IMPORT_KIND, on_setup_import_progress)
    register_worker(cloud_job.JOB_KIND, cloud_job.build_argv)
    register_reconcile(cloud_job.JOB_KIND, cloud_job.reconcile_from_disk)


# ----- legacy composition ------------------------------------------------
def _compose_legacy(cfg: EngineConfig) -> FastAPI:
    """Mirror of ``apps.webui.server.app._build_default_app`` with the SPA
    mount deferred, because the chassis has routes to add after it.

    Deliberate difference: the legacy factory swallows a SqliteBackend
    construction failure and warns its way into an InMemoryBackend. That is
    exactly the silent fallback the house rules ban -- a backend that failed
    to open must stop the boot, not quietly serve an empty library.
    ``make_backend`` already returns InMemoryBackend BY DESIGN when state.db
    is simply absent, so failing fast here costs nothing legitimate.
    """
    apply_library_env()
    platform_paths.refresh_share_root()
    assert_ready()
    stems = stem_storage()
    ensure_stem_storage(stems)
    state_db_path = _create_state_store(cfg)

    return legacy_create_app(
        backend=make_backend(state_db_path),
        bind_host=cfg.host,
        hostname=os.environ.get("MUSIC_DJ_HOSTNAME"),
        syncthing_status_fn=probe_syncthing_status,
        state_db_path=str(state_db_path),
        port=cfg.port,
        stem_roots=stems.roots,
        mount_frontend=False,
        # Browser diagnostics belong to THIS engine's data dir. The legacy
        # default is a process-global path under $HOME, which left an engine
        # started with --data-dir writing outside its own sandbox and two
        # parallel lanes appending to one another's log.
        client_error_log_dir=cfg.logs_dir,
        client_event_log_dir=cfg.logs_dir,
        # Analyze-on-import: the engine is the long-lived desktop daemon, so
        # it is the process that reconciles the rekordbox-unmapped backlog.
        # Same MUSIC_DJ_AUTO_ANALYZE contract as the standalone webui daemon,
        # and arm_from_environ rather than enabled_from_environ because the
        # PACKAGED engine ships the --no-dev closure: librosa and scipy stay
        # in the optional analysis extra, so the installed app must decline to
        # arm a loop whose every drain would raise BackendNotAvailable.
        auto_analyze=analysis_autostart.arm_from_environ(os.environ),
        # FBSYNC-01: the desktop engine is where pins must sync with no manual
        # step, so it arms the CloudSync scheduler. Armed is not running: it
        # still waits for MDT_CLOUDSYNC_SCHEDULER=1 and MDT_CLOUDSYNC_HUB_URL.
        cloudsync_scheduler=True,
        # User-ordered stems and lyrics jobs (/api/v1/library-jobs) only run if
        # the daemon drains them. Same MUSIC_DJ_LIBRARY_JOBS contract as the
        # standalone webui daemon; without it the installed app queued jobs
        # that stayed pending forever.
        auto_user_jobs=library_jobs_autostart.arm_from_environ(os.environ),
        # STEM-31 / ADR-0051: on-demand stem hydration (ADR-0024). Armed is not
        # running: it stays inert in local mode or when hydration cannot arm.
        stem_hydration=True,
    )


def _create_state_store(cfg: EngineConfig) -> Path:
    """Create and migrate ``state.db`` BEFORE a backend is chosen (#3965).

    ``make_backend`` picks by file presence: SqliteBackend when ``state.db``
    exists, InMemoryBackend when it does not. On a first run nothing had
    created it yet, so the engine bound an in-memory library for the life of
    the process. The first folder import then wrote its rows to sqlite,
    readiness (which reads sqlite directly) counted them, and ``/tracks`` and
    ``/health`` served an empty library until the app was relaunched. The
    availability worker created the file a moment after boot anyway, too
    late, and a readiness request racing that creation read a half-migrated
    schema and 500'd.

    The engine is the long-lived store owner, so an in-memory library is
    never its right answer: anything written to one is lost at exit. This
    writes no rows. An empty, fully migrated store is what every later
    reader and the import worker expect to find.
    """
    path = cfg.state_db
    state_db.open_rw(path).close()
    return path


def _drop_api_routes(app: FastAPI, prefix: str) -> int:
    kept = [
        route
        for route in app.router.routes
        if not (isinstance(route, APIRoute) and route.path.startswith(prefix))
    ]
    dropped = len(app.router.routes) - len(kept)
    app.router.routes[:] = kept
    return dropped


def _drop_root_placeholder(app: FastAPI) -> None:
    """Drop the legacy "/" JSON placeholder so the SPA mount can own "/".

    The legacy composition registers it even with ``mount_frontend=False``,
    and Starlette matches routes before mounts, so it would shadow the SPA
    at exactly "/". Drop-if-present, not drop-or-raise: whether legacy
    registers it depends on its own flags.
    """
    app.router.routes[:] = [
        route
        for route in app.router.routes
        if not (isinstance(route, APIRoute) and route.path == "/")
    ]


def _drop_or_raise(app: FastAPI, prefix: str, what: str) -> None:
    if _drop_api_routes(app, prefix) == 0:
        raise EngineBootError(
            f"expected to find the {what} at {prefix} but there are no routes "
            "there; the legacy composition changed shape"
        )


def _add_health_route(app: FastAPI) -> None:
    """Wrap the legacy handler rather than editing apps/webui.

    ``legacy_health`` is called unchanged and its response model is
    subclassed, so a legacy health fix lands here for free.
    """

    @app.get(
        HEALTH_PATH,
        response_model=EngineHealthOut,
        tags=["health"],
        name="engine_health",
    )
    def engine_health(
        request: Request,
        backend: Annotated[StateBackend, Depends(get_read_state)],
    ) -> EngineHealthOut:
        base = legacy_health(request, backend)
        return EngineHealthOut(
            **base.model_dump(),
            contract_rev=request.app.state.contract_rev,
            engine_version=ENGINE_VERSION,
            boot_id=request.app.state.engine_boot_id,
        )


def _mount_spa(app: FastAPI) -> None:
    """Mount the SPA build dir exactly as apps/webui/server/app.py does."""
    build_dir = frontend_build_dir()
    if build_dir.exists() and any(build_dir.iterdir()):
        app.mount(
            "/",
            _SpaStaticFiles(directory=str(build_dir), html=True),
            name="spa",
        )
        return

    @app.get("/", include_in_schema=False)
    def index_placeholder() -> dict[str, str]:
        return {
            "status": "ok",
            "message": (
                "opendj engine running without an SPA build. Build the "
                "frontend (cd apps/webui/frontend && pnpm install && "
                "pnpm build) to serve the UI here."
            ),
            "api_docs": "/docs",
            "openapi": "/openapi.json",
        }


# ----- lifespan ----------------------------------------------------------
def _wrap_lifespan(
    app: FastAPI,
    *,
    cfg: EngineConfig,
    hub: WsHub,
    store: JobStore,
    runner: JobRunner,
    lock: EngineLock | None,
    availability_worker: LibraryAvailabilityWorker,
    audio_engine: AudioEngineSupervisor,
) -> None:
    """Wrap, never replace, the legacy lifespan.

    FastAPI ignores ``on_startup``/``on_shutdown`` once a lifespan context
    exists, so registering handlers after the fact would fail silently.
    """
    legacy_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def _lifespan(instance: FastAPI) -> AsyncIterator[None]:
        hub.bind(asyncio.get_running_loop())
        events.set_hub(hub)
        restore_hub_publish = attach_library_changed_probe(availability_worker, hub)
        await runner.start()
        heartbeat = (
            asyncio.create_task(_heartbeat(lock)) if lock is not None else None
        )
        hub.publish(
            TOPIC_HEALTH_CHANGED,
            {
                "status": "ok",
                "contract_rev": hub.contract_rev,
                "engine_version": hub.engine_version,
                "boot_id": instance.state.engine_boot_id,
                "data_dir": str(cfg.data_dir),
            },
        )
        try:
            # The CloudSync scheduler idles until cloudsync-config.json (or
            # its env overrides) turns it on, and never starts on the hub.
            # First run (#3870): a build that names a default hub writes that
            # file once, so a test user's install syncs with no prompt. An
            # existing file always wins; no default means nothing changes.
            cloudsync_dir = Path(str(instance.state.state_db_path)).resolve().parent.parent
            cloudsync_first_run.seed_default_config(cloudsync_dir, env=os.environ)
            async with (
                legacy_lifespan(instance),
                scheduler_lifespan(
                    cloudsync_dir,
                    ui_mirror_provider=lambda: getattr(instance.state, "ui_mirror", None),
                ) as sched,
                # cfg.data_dir, not cloudsync_dir: same per-instance value
                # ``LibraryAvailabilityWorker(cfg.data_dir)`` above already
                # uses, rather than the module-level ``STATE_DB`` constant
                # ``cloudsync_dir`` is derived from (out of scope for #3180
                # to also re-key CloudSync onto it).
                folder_rescan_lifespan(cfg.data_dir) as folder_rescan,
            ):
                instance.state.sync_hub_scheduler = sched
                instance.state.folder_rescan_scheduler = folder_rescan
                availability_worker.start()
                audio_engine.autostart()
                try:
                    yield
                finally:
                    # Off the event loop: stop() waits for the process.
                    await asyncio.to_thread(audio_engine.stop)
                    availability_worker.stop()
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            # Jobs stop while the hub is still live, so a client that is
            # still connected sees the cancellations instead of inferring
            # them from a dropped socket.
            #
            # try/finally, because a stop() that raises must not strand the
            # db handle and the hub binding: the process is going away either
            # way, and a leaked connection on the jobs db is what makes the
            # NEXT boot fight for the lock.
            try:
                await runner.stop()
            finally:
                hub.publish = restore_hub_publish  # type: ignore[method-assign]
                events.set_hub(None)
                hub.unbind()
                store.close()

    app.router.lifespan_context = _lifespan


async def _heartbeat(
    lock: EngineLock, interval_s: float = HEARTBEAT_INTERVAL_S
) -> None:
    while True:
        await asyncio.sleep(interval_s)
        lock.heartbeat()


__all__ = [
    "EVENTS_PATH",
    "HEALTH_PATH",
    "PROGRESS_PREFIX",
    "EngineHealthOut",
    "create_app",
]
