"""``create_app(cfg)`` -- the engine chassis around the legacy routers.

The legacy FastAPI composition is REUSED, not reimplemented: this module
calls ``apps.webui.server.app.create_app`` and then applies three
chassis-level edits.

1. The progress router is removed. The engine does not serve the fan-out
   ledger, and boot refuses outright if a progress-tree.yaml is sitting in
   the data dir.
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

from apps.engine_core.config import (
    ENGINE_VERSION,
    EngineBootError,
    EngineConfig,
    assert_no_progress_ledger,
    prepare_layout,
)
from apps.engine_core.contract import compute_contract_rev
from apps.engine_core.jobs.api import router as jobs_router
from apps.engine_core.jobs.runner import JobRunner
from apps.engine_core.jobs.store import JobStore
from apps.engine_core.lock import EngineLock
from apps.engine_core.ws import TOPIC_HEALTH_CHANGED, WsHub, events_endpoint
from apps.shared import events, platform_paths
from apps.shared.library_mode import apply_library_env, assert_ready
from apps.shared.paths import STATE_DB
from apps.webui.library_assets import ensure_stem_storage, stem_storage
from apps.webui.server.app import FRONTEND_BUILD_DIR, _SpaStaticFiles
from apps.webui.server.app import create_app as legacy_create_app
from apps.webui.server.backend import StateBackend
from apps.webui.server.cloud_sync import probe_syncthing_status
from apps.webui.server.deps import get_read_state
from apps.webui.server.models import HealthOut
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
    assert_no_progress_ledger(cfg.data_dir)
    prepare_layout(cfg)

    boot_id = lock.boot_id if lock is not None else str(uuid.uuid4())
    app = _compose_legacy(cfg)

    _drop_or_raise(app, PROGRESS_PREFIX, "legacy progress router")
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
    app.include_router(jobs_router, prefix=API_PREFIX)
    app.add_api_websocket_route(EVENTS_PATH, events_endpoint, name="events")

    _drop_root_placeholder(app)
    _mount_spa(app)

    contract_rev = compute_contract_rev(app.openapi())
    hub = WsHub(contract_rev=contract_rev, engine_version=ENGINE_VERSION)

    app.state.engine_cfg = cfg
    app.state.engine_boot_id = boot_id
    app.state.contract_rev = contract_rev
    app.state.jobs_store = store
    app.state.jobs_runner = runner
    app.state.event_hub = hub

    _wrap_lifespan(app, cfg=cfg, hub=hub, store=store, runner=runner, lock=lock)
    return app


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

    return legacy_create_app(
        backend=make_backend(),
        bind_host=cfg.host,
        hostname=os.environ.get("MUSIC_DJ_HOSTNAME"),
        syncthing_status_fn=probe_syncthing_status,
        state_db_path=str(STATE_DB),
        port=cfg.port,
        stem_roots=stems.roots,
        mount_frontend=False,
    )


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
    build_dir: Path = FRONTEND_BUILD_DIR
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
            async with legacy_lifespan(instance):
                yield
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
