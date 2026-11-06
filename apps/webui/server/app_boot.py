"""Daemon boot path for the webui FastAPI app.

``create_app`` stays the public factory in :mod:`apps.webui.server.app`.
This module owns ``_build_default_app``, which opens the real ``state.db``.
Importing it must not build an app or run migrations.
"""

from __future__ import annotations

from fastapi import FastAPI


def _build_default_app() -> FastAPI:
    import os
    from pathlib import Path

    from apps.shared import platform_paths
    from apps.shared.library_mode import apply_library_env, assert_ready
    from apps.shared.sync_bind_guard import assert_sync_bind_allowed
    from apps.webui.library_assets import ensure_stem_storage, stem_storage
    from apps.webui.server.request_guard import assert_request_guard_bind_allowed

    from . import (
        ahead_analysis,
        analysis_autostart,
        boot_grace,
        coverage_drain,
        grid_phase_precompute,
        library_jobs_autostart,
        lyric_index_autostart,
    )
    from .app import create_app
    from .cloud_sync import probe_syncthing_status
    from .dev_cloudsync_guard import assert_dev_cloudsync_allowed
    from .routes import reconcile as reconcile_routes

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
    # Phase 5 wiring: prefer SqliteBackend when ``(private path)`` exists,
    # else fall back to the in-memory backend (keeps dev + tests fast).
    # ``make_backend()`` itself already returns InMemoryBackend when no db
    # file exists; it only raises for a real construction/migration failure,
    # and that must abort this boot path too (#762) rather than silently
    # swallow into an empty in-memory library.
    from .backend import StateBackend
    from .sqlite_backend import make_backend

    backend: StateBackend = make_backend()
    # Late import, same reason as make_backend()'s: apply_library_env() above
    # must run first so STATE_DB (re-exported from platform_paths.DATA_DIR)
    # reflects this process's MDT_DATA_DIR rather than whatever value it
    # froze to at whatever module happened to import it first.
    #
    # Without this, create_app()'s own default for state_db_path is the
    # literal "(private path)", resolved relative to CWD rather than
    # MDT_DATA_DIR. That mismatch was silent until debc73644 started binding
    # app.state.analysis_db_path to state_db_path unconditionally: before
    # that commit, the analysis routes' _analysis_db_path() saw no override
    # (getattr returned None) and fell back to STATE_DB directly, so they
    # happened to be correct by omission. After it, the real daemon's
    # analysis/beatgrid-fallback routes read the wrong file whenever
    # MDT_DATA_DIR diverges from CWD/data -- e.g. every e2e suite that boots
    # this entrypoint against a fixture data dir with an unmapped track (#949).
    from apps.shared.paths import STATE_DB
    # CLOUDSYNC-35: a cloned data dir syncs as the machine it was cloned from;
    # refuse before the scheduler is armed, never strip the config afterwards.
    assert_dev_cloudsync_allowed(Path(str(STATE_DB)).resolve().parent.parent, os.environ)
    app = create_app(
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
        # NATIVE-21: the ahead-of-time analysis drain; create_app leaves it off.
        auto_ahead_analysis=ahead_analysis.arm_from_environ(os.environ),
        auto_grid_phase_precompute=grid_phase_precompute.arm_from_environ(os.environ),
        stem_hydration=True,
    )
    # PERF-BOOT-01: the background scanners hold their first pass until the
    # launch's library index is served, or BOOT_GRACE_S.
    boot_grace.arm(app)
    # HEALTH-15: the daemon's first reconcile scan starts now, not on the
    # browser's first read (39 to 61 s on the silver preview under load).
    reconcile_routes.warm_summary_snapshot(app, backend)
    return app
