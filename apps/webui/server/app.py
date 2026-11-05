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
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from apps.feature_flags import FlagStore
from apps.sets.share import SetShareConfig
from apps.sets.share_page import router as set_share_page_router
from apps.shared.app_version import APP_VERSION
from apps.shared.bounded_file_open import AUDIO_ACCESS_TIMEOUT_S

from . import (
    analysis_serving_bootstrap,  # noqa: F401 - PARITY-02 lane registration
    library_jobs_autostart,
)
from .app_boot import _build_default_app
from .app_watchers import (  # noqa: F401  # re-exported for tests and inspect.getsource parity
    build_auto_analyze_watcher,
    build_lyric_index_watcher,
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
from .frontend_build import frontend_build_dir
from .request_guard import install_request_guard
from .share_gate import ShareConfig
from .usage_telemetry import UsageStore

log = logging.getLogger(__name__)

FRONTEND_BUILD_DIR: Path = frontend_build_dir()

#: The OpenAPI document's own ``info.version``: the version of the HTTP
#: contract in ``apps/webui/openapi.json``, which changes when the contract
#: does. It is not the app version (``version`` below, from the app package.json):
#: tying the two together would make every release bump rewrite the committed
#: schema and trip the contract-drift gate.
OPENAPI_DOCUMENT_VERSION: str = "0.1.0"


def create_app(  # noqa: PLR0913
    *,
    backend: StateBackend | None = None,
    bind_host: str = "127.0.0.1",
    hostname: str | None = None,
    lock_status_fn: Callable[[], Any] | None = None,
    syncthing_status_fn: Callable[[], Any] | None = None,
    state_db_path: str = "(private path)",
    version: str = APP_VERSION,
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
    auto_ahead_analysis: bool = False,
    feature_flags: FlagStore | None = None,
    cloudsync_scheduler: bool = False,
    stem_hydration: bool = False,
    audio_access_timeout_s: float = AUDIO_ACCESS_TIMEOUT_S,
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

    ``audio_access_timeout_s`` is how long ``GET /tracks/{id}/audio`` gives
    its subprocess open probe (#2770) before answering 503
    ``AUDIO_ACCESS_BLOCKED``. Every daemon keeps the product default,
    :data:`apps.shared.bounded_file_open.AUDIO_ACCESS_TIMEOUT_S`; a test that
    needs a 200 on a loaded CI runner passes a longer one here (W45) instead
    of patching the constant.
    """

    port, frontend_port = _resolve_ports(port, frontend_port)
    app = FastAPI(
        title="music-dj-tools webui",
        version=OPENAPI_DOCUMENT_VERSION,
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
    app.state.ahead_analysis_armed = auto_ahead_analysis
    app.state.frontend_port = frontend_port
    if audio_access_timeout_s <= 0:
        raise ValueError(f"audio_access_timeout_s must be > 0, got {audio_access_timeout_s}")
    app.state.audio_access_timeout_s = audio_access_timeout_s
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
