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
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from apps.feature_flags import FlagStore
from apps.sets.share import SetShareConfig
from apps.sets.share_page import router as set_share_page_router

from . import (
    analysis_autostart,
    analysis_serving_bootstrap,  # noqa: F401 - PARITY-02 lane registration
    coverage_drain,
    library_jobs_autostart,
    lyric_index_autostart,
)
from .app_wiring import (
    _bind_core_state,
    _bind_feature_state,
    _bind_stem_and_usage,
    _bind_stem_hydration,
    _configure_cors,
    _configure_http_middleware,
    _install_exception_handlers,
    _lifespan_context,
    _mount_api_routers,
    _mount_frontend_or_placeholder,
    _resolve_ports,
    _SpaStaticFiles,
)
from .backend import StateBackend
from .cloud_sync import probe_syncthing_status
from .frontend_build import frontend_build_dir
from .request_guard import install_request_guard
from .routes import ingest as ingest_routes
from .share_gate import ShareConfig
from .usage_telemetry import UsageStore

log = logging.getLogger(__name__)

FRONTEND_BUILD_DIR: Path = frontend_build_dir()


def create_app(  # noqa: PLR0913
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
    sets_root: Path | None = None,
    auto_analyze: bool = False,
    lyric_index: bool = False,
    auto_user_jobs: bool = False,
    auto_coverage_drain: bool = False,
    feature_flags: FlagStore | None = None,
    cloudsync_scheduler: bool = False,
    stem_hydration: bool = False,
) -> FastAPI:
    """Build a configured FastAPI app.

    ``auto_analyze`` arms the analyze-on-import reconcile loop (see
    :mod:`apps.webui.server.analysis_autostart`). It is OFF here on purpose:
    the loop shells out to ``apps.analysis.run``, so only the real daemon
    entry point (``_build_default_app``) turns it on, from
    ``MUSIC_DJ_AUTO_ANALYZE``. Tests opt in explicitly.

    ``lyric_index`` arms the pausable background lyric-index reconcile loop
    (see :mod:`apps.webui.server.lyric_index_autostart`). It is OFF here on
    purpose too: only ``_build_default_app`` turns it on, from
    ``MUSIC_DJ_LYRIC_INDEX``, so no test builds a thread.

    ``cloudsync_scheduler`` arms the CloudSync scheduler (FBSYNC-01, see
    :mod:`apps.webui.server.cloudsync_scheduler`). OFF here for the same
    reason: only the daemon entry points arm it, and even armed it runs only
    when ``MDT_CLOUDSYNC_SCHEDULER=1`` and a hub URL are set.

    ``stem_hydration`` arms on-demand R2 stem-bundle hydration (ADR-0024, see
    :mod:`apps.cloud.stem_hydration`). Same shape as ``cloudsync_scheduler``:
    OFF here so no test spins up a thread pool or reaches for credentials,
    and even armed it stays inert unless CloudSync is in ``cloud`` mode AND
    R2 credentials resolve (``_bind_stem_hydration``).

    ``feature_flags`` is UNLIKE those two: it is wired here, not left for
    ``_build_default_app``, because ``load_flags`` reads one small on-disk
    file with no side effect worth deferring, and a USB route gated on
    ``app.state.feature_flags`` (SAND-01) must not 500 on the daemon this
    app boots directly (``python -m apps.webui.server``, the legacy entry
    point that never passes through ``apps.engine_core.app.create_app``).
    Pass an explicit ``FlagStore`` to pin the flags a test resolves against;
    the default reads ``apps.shared.paths.DATA_DIR``.
    """

    port, frontend_port = _resolve_ports(port, frontend_port)
    app = FastAPI(
        title="music-dj-tools webui",
        version=version,
        lifespan=_lifespan_context,
        description=(
            "Local-first web UI for music-dj-tools. Binds to 127.0.0.1 by "
            "default (D5 / CAT-05b). Override via MUSIC_DJ_BIND_HOST."
        ),
    )
    _bind_core_state(
        app,
        backend,
        bind_host,
        hostname,
        port,
        lock_status_fn,
        syncthing_status_fn,
        state_db_path,
    )
    app.state.set_share_config = set_share_config or SetShareConfig.from_environ()
    app.state.sets_root = Path(sets_root) if sets_root is not None else None
    _bind_feature_state(
        app,
        version,
        feature_flags,
        share_config,
        auto_analyze,
        lyric_index,
        client_error_log_dir,
        client_event_log_dir,
    )
    _bind_stem_and_usage(app, stem_roots, usage_store)
    _bind_stem_hydration(
        app, data_dir=Path(state_db_path).resolve().parent.parent, enabled=stem_hydration
    )
    app.state.cloudsync_scheduler_armed = cloudsync_scheduler
    app.state.auto_user_jobs = library_jobs_autostart.build(enabled=auto_user_jobs)
    app.state.coverage_drain_armed = auto_coverage_drain
    _install_exception_handlers(app)
    install_request_guard(
        app,
        frontend_port=frontend_port,
        backend_port=port,
        enable_cors=enable_cors,
    )
    if enable_cors:
        _configure_cors(app)
    _configure_http_middleware(app, bind_host)
    _mount_api_routers(app)
    app.include_router(set_share_page_router)
    _mount_frontend_or_placeholder(app, mount_frontend, FRONTEND_BUILD_DIR)
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
            ingest_routes._stem_roots(app),
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


def build_lyric_index_watcher(app: FastAPI) -> lyric_index_autostart.LyricIndexWatcher:
    """Bind the lyric-index reconcile loop to this app's data and usage store.

    Public because it IS the wiring under test: ``create_app`` supplies
    ``state_db_path`` (whose grandparent directory is the data root that
    holds ``state/`` - the lyrics cache and the index file both live under
    ``data_dir/state/``) and ``usage_store`` (the UI-activity probe that
    pauses the job while someone is using the app).
    """
    state_db_path = Path(app.state.state_db_path).resolve()
    data_dir = state_db_path.parent.parent
    # A layout mismatch (state_db_path not exactly two levels under the data
    # root) makes the derivation above point at the wrong tree: the watcher
    # would then find zero candidates, write the index into that wrong tree,
    # and report idle forever with no error. Fail loud instead - but only
    # when the daemon is actually armed to do that reconcile work, so an app
    # built with the feature off (the default in every test but the lyric
    # daemon's own) is free to pass a state_db_path with no such layout.
    if app.state.lyric_index.enabled and (
        state_db_path.parent.name != "state" or not (data_dir / "state").is_dir()
    ):
        raise RuntimeError(
            f"lyric index data_dir {data_dir} guessed from state_db_path "
            f"{state_db_path} does not match the required data_dir/state/"
            "state.db layout; refusing to reconcile a directory that cannot "
            "hold the lyrics cache."
        )

    def ui_active() -> bool:
        # "is the app open and on screen" is the DJ-safe pause signal: index
        # work never runs while a client could be at the decks.
        return bool(
            app.state.usage_store.snapshot()["summary"]["any_client_in_use"]
        )

    return lyric_index_autostart.LyricIndexWatcher(
        app.state.lyric_index, data_dir=data_dir, activity_fn=ui_active
    )


def _build_default_app() -> FastAPI:
    from apps.shared import platform_paths
    from apps.shared.library_mode import apply_library_env, assert_ready
    from apps.shared.sync_bind_guard import assert_sync_bind_allowed
    from apps.webui.library_assets import ensure_stem_storage, stem_storage
    from apps.webui.server.request_guard import assert_request_guard_bind_allowed

    bind_host = os.environ.get("MUSIC_DJ_BIND_HOST", "127.0.0.1")
    # W3, before any disk work: this app mounts /api/v1/sync/*. Covers the bare
    # `uvicorn ...app:app` entry via MUSIC_DJ_BIND_HOST; uvicorn's own --host
    # never reaches the app, so that flag alone is unguarded (sync_bind_guard).
    assert_sync_bind_allowed(bind_host)
    assert_request_guard_bind_allowed(bind_host)
    apply_library_env()
    platform_paths.refresh_share_root()
    assert_ready()
    stems = stem_storage()
    ensure_stem_storage(stems)
    hostname = os.environ.get("MUSIC_DJ_HOSTNAME")
    # Phase 5 wiring: prefer SqliteBackend when ``data/state/state.db`` exists,
    # else fall back to the in-memory backend (keeps dev + tests fast).
    # ``make_backend()`` itself already returns InMemoryBackend when no db
    # file exists; it only raises for a real construction/migration failure,
    # and that must abort this boot path too (#762) rather than silently
    # swallow into an empty in-memory library.
    from .sqlite_backend import make_backend

    backend: StateBackend = make_backend()
    # Late import, same reason as make_backend()'s: apply_library_env() above
    # must run first so STATE_DB (re-exported from platform_paths.DATA_DIR)
    # reflects this process's MDT_DATA_DIR rather than whatever value it
    # froze to at whatever module happened to import it first.
    #
    # Without this, create_app()'s own default for state_db_path is the
    # literal "data/state/state.db", resolved relative to CWD rather than
    # MDT_DATA_DIR. That mismatch was silent until debc73644 started binding
    # app.state.analysis_db_path to state_db_path unconditionally: before
    # that commit, the analysis routes' _analysis_db_path() saw no override
    # (getattr returned None) and fell back to STATE_DB directly, so they
    # happened to be correct by omission. After it, the real daemon's
    # analysis/beatgrid-fallback routes read the wrong file whenever
    # MDT_DATA_DIR diverges from CWD/data -- e.g. every e2e suite that boots
    # this entrypoint against a fixture data dir with an unmapped track (#949).
    from apps.shared.paths import STATE_DB
    return create_app(
        backend=backend,
        bind_host=bind_host,
        hostname=hostname,
        syncthing_status_fn=probe_syncthing_status,
        stem_roots=stems.roots,
        state_db_path=str(STATE_DB),
        auto_analyze=analysis_autostart.arm_from_environ(os.environ),
        lyric_index=lyric_index_autostart.enabled_from_environ(os.environ),
        cloudsync_scheduler=True,
        auto_user_jobs=library_jobs_autostart.arm_from_environ(os.environ),
        auto_coverage_drain=coverage_drain.arm_from_environ(os.environ),
        stem_hydration=True,
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


__all__ = ["FRONTEND_BUILD_DIR", "_SpaStaticFiles", "app", "create_app", "create_process_app"]  # noqa: F822 -- "app" is a PEP 562 lazy attribute, not a real binding
