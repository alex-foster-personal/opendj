"""``python -m apps.engine_core serve --data-dir PATH --port N [--host H] [--machine-name NAME]``.

argparse rather than typer: typer is not in the repo venv, and a CLI that
needs a new dependency to print its own help is not a chassis.

Ordering here is the whole point of the module. The env contract is applied
BEFORE the first ``apps.webui`` import, because the legacy modules resolve
their paths at import time; importing them first would bind them to the
wrong data dir with no error anywhere. The engine lock is taken before the
app is built, so a second engine refuses early instead of after the socket
is bound.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from apps.engine_core.config import (
    ENGINE_VERSION,
    EngineBootError,
    EngineConfig,
    apply_env_contract,
    assert_single_worker,
    build_config,
    prepare_layout,
)
from apps.engine_core.lock import EngineLock, EngineLockError
from apps.engine_core.parent_watch import start as start_parent_watch
from apps.feature_flags.profiles import (
    BUILD_PROFILE_ENV,
    UnknownProfileError,
    available_profiles,
    profile_path,
)
from apps.shared.google_oauth_client import apply_bundled_oauth
from apps.shared.sync_bind_guard import SyncBindRefused, assert_sync_bind_allowed

EXIT_OK: int = 0
EXIT_LOCKED: int = 1
EXIT_REFUSED: int = 2
HUB_MACHINE_NAME_ENV: str = "MDT_HUB_MACHINE_NAME"
#: Seconds uvicorn waits for open requests on SIGTERM before it closes them
#: and runs the lifespan shutdown. See the uvicorn.run call below.
GRACEFUL_SHUTDOWN_S: int = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m apps.engine_core",
        description=f"opendj engine {ENGINE_VERSION}",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the engine")
    serve.add_argument(
        "--data-dir", required=True, help="absolute path to the library data dir"
    )
    serve.add_argument("--port", required=True, type=int)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument(
        "--workers",
        type=int,
        default=1,
        help="single-worker by design; anything else is refused",
    )
    serve.add_argument("--log-level", default="info")
    serve.add_argument(
        "--build-profile",
        default=None,
        help=(
            "named feature-flag profile for this boot, e.g. 'appstore'. "
            f"One of: {', '.join(available_profiles())}. Sets "
            f"{BUILD_PROFILE_ENV} for the process; an explicit "
            "MDT_FEATURE_FLAGS_FILE still wins. An unknown name is refused, "
            "never defaulted to the full build."
        ),
    )
    serve.add_argument(
        "--machine-name",
        default=os.environ.get(HUB_MACHINE_NAME_ENV),
        help=(
            "this hub's CloudSync display/registration name "
            f"(default: ${HUB_MACHINE_NAME_ENV} when set, otherwise the "
            "short hostname at first sync)"
        ),
    )
    rescue = sub.add_parser("rescue", help="list or restore Gig performance snapshots")
    rescue_sub = rescue.add_subparsers(dest="rescue_command", required=True)
    rescue_list = rescue_sub.add_parser("list", help="GET /api/v1/rescue/snapshots")
    rescue_list.add_argument("--data-dir", required=True)
    rescue_list.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MUSIC_DJ_BACKEND_PORT", "8585")),
    )
    rescue_list.add_argument("--json", action="store_true")
    rescue_restore = rescue_sub.add_parser(
        "restore", help="POST /api/v1/rescue/restore"
    )
    rescue_restore.add_argument("--data-dir", required=True)
    rescue_restore.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MUSIC_DJ_BACKEND_PORT", "8585")),
    )
    rescue_restore.add_argument("--play", action="store_true")
    rescue_restore.add_argument("--snapshot-id")
    rescue_restore.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "rescue":
        from apps.engine_core.rescue_cli import run_rescue

        rescue_argv = [args.rescue_command]
        rescue_argv.extend(["--data-dir", args.data_dir])
        rescue_argv.extend(["--port", str(args.port)])
        if getattr(args, "json", False):
            rescue_argv.append("--json")
        if getattr(args, "play", False):
            rescue_argv.append("--play")
        snapshot_id = getattr(args, "snapshot_id", None)
        if snapshot_id:
            rescue_argv.extend(["--snapshot-id", snapshot_id])
        return run_rescue(rescue_argv)
    try:
        _apply_build_profile(args.build_profile)
        cfg = build_config(args.data_dir, args.host, args.port)
        _preflight(cfg, workers=args.workers)
    except EngineBootError as exc:
        print(f"[ERROR] engine refused to boot: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    return _serve(cfg, log_level=args.log_level, machine_name=args.machine_name)


def _apply_build_profile(name: str | None) -> None:
    """Publish ``--build-profile`` to the environment, validating it first.

    Set as an env var rather than threaded through EngineConfig because the
    flag store is read at import-adjacent startup by ``create_app`` and by any
    subprocess this boot spawns, and one env var reaches all of them. Validated
    HERE so a typo dies at argument parsing with the list of real profiles,
    rather than at flag-load time inside the app factory.
    """
    if name is None:
        return
    try:
        profile_path(name)
    except UnknownProfileError as exc:
        raise EngineBootError(str(exc)) from exc
    os.environ[BUILD_PROFILE_ENV] = name


def _assert_sync_bind(host: str) -> None:
    """The engine mounts ``/api/v1/sync/*``: a wide bind needs explicit trust.

    Checked here, before the lock and the socket, so a refused bind exits
    EXIT_REFUSED with the ADR named instead of after the port is open.
    """
    try:
        assert_sync_bind_allowed(host)
    except SyncBindRefused as exc:
        raise EngineBootError(str(exc)) from exc


def _preflight(cfg: EngineConfig, *, workers: int) -> None:
    assert_single_worker(workers)
    _assert_sync_bind(cfg.host)
    if not cfg.data_dir.is_dir():
        # Never invent a library root: a typo in --data-dir would otherwise
        # produce a silently empty library that looks like a real one.
        raise EngineBootError(
            f"--data-dir {cfg.data_dir} does not exist. Create it first; the "
            "engine only creates what it owns (state/, jobs.db, .engine.lock)."
        )
    apply_env_contract(cfg)
    apply_bundled_oauth(os.environ)
    prepare_layout(cfg)


def _telemetry_decision(data_dir: Path | None = None):
    """Ask the build what it is, then decide whether to report errors.

    This glue lives here rather than in apps.shared.telemetry because that
    module has to stay a leaf: apps.engine_core imports apps.webui, and
    apps.webui imports the telemetry module, so a build_info import from
    inside it would close a cycle.

    A build that cannot describe itself is treated as a checkout rather than
    as a fault. Telemetry is the diagnostic, not the product, so it declines
    to report rather than taking the boot down with it -- and a checkout
    defaults OFF, so declining is also the quiet, safe direction.
    """
    from apps.engine_core.build_info import (
        BuildInfoUnavailable,
        resolve_build_info,
    )
    from apps.shared import platform_paths
    from apps.shared.telemetry import decide_telemetry
    from apps.shared.telemetry.bundled import (
        BundledTelemetryError,
        load_bundled_telemetry,
        opt_out_reason,
    )
    from apps.shared.telemetry.consent import declined_reason

    try:
        info = resolve_build_info(dict(os.environ), platform_paths.PROJECT_ROOT)
        source, release = info.source, info.git_sha_full
        if release:
            os.environ.setdefault("OPENDJ_BUILD_SHA", release)
    except BuildInfoUnavailable as exc:
        print(
            f"[WARN] build identity unavailable ({exc}); telemetry treats this "
            "as a checkout and will not tag a release",
            file=sys.stderr,
        )
        source, release = None, None
    # The dmg's own DSN (OBS-04). A launcher that names a file the engine
    # cannot read is a damaged install: loud, and treated as "no bundle" so
    # the tester's app still runs. The payload build verified the file, so
    # this never describes a build that shipped.
    try:
        bundled = load_bundled_telemetry(os.environ)
    except BundledTelemetryError as exc:
        print(f"[ERROR] bundled telemetry unreadable: {exc}", file=sys.stderr)
        bundled = None
    return decide_telemetry(
        os.environ,
        build_source=source,
        release=release,
        bundled_dsn=bundled.dsn if bundled is not None else None,
        # The marker file, else a stored "declined" answer to the terms.
        opt_out=opt_out_reason(data_dir) or declined_reason(data_dir),
    )


def _serve(cfg: EngineConfig, *, log_level: str, machine_name: str | None) -> int:
    lock = EngineLock(cfg.lock_path, host=cfg.host, port=cfg.port)
    try:
        lock.acquire()
    except EngineLockError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return EXIT_LOCKED
    start_parent_watch(cfg.data_dir)
    try:
        from apps.shared.process_identity import set_process_identity

        set_process_identity(
            "Engine", cfg.port, invocation_marker="apps.engine_core serve"
        )
        # Imported here, never at module scope: this pulls in apps.webui,
        # which reads MDT_DATA_DIR at import time.
        import uvicorn

        from apps.engine_core.access_log import configure_access_log_sampling
        from apps.engine_core.app import create_app
        from apps.engine_core.warning_log import configure_warning_log
        from apps.shared.telemetry import TelemetryConfigError, init_telemetry

        warning_log = os.environ.get("OPENDJ_ENGINE_WARN_LOG")
        warning_boot_id = os.environ.get("OPENDJ_ENGINE_LOG_BOOT_ID")
        if (warning_log is None) != (warning_boot_id is None):
            raise EngineBootError(
                "OPENDJ_ENGINE_WARN_LOG and OPENDJ_ENGINE_LOG_BOOT_ID must be set together"
            )
        if warning_log is not None:
            logging.basicConfig(level=logging.INFO)
            configure_warning_log(Path(warning_log), warning_boot_id)
        configure_access_log_sampling()

        # BEFORE create_app, not after: the Sentry FastAPI integration wraps
        # route handlers as they are registered, so a later init would leave
        # every route already built and silently uninstrumented.
        try:
            from apps.shared.telemetry.consent import read_consent

            init_telemetry(
                _telemetry_decision(cfg.data_dir),
                consent_granted=read_consent(cfg.data_dir).decision == "accepted",
            )
        except TelemetryConfigError as exc:
            # Asked for by name and undeliverable. Refusing here is the whole
            # point: booting anyway would mean the errors somebody is waiting
            # on never arrive and nothing ever says so.
            print(f"[ERROR] telemetry refused to start: {exc}", file=sys.stderr)
            return EXIT_REFUSED

        app = create_app(cfg, lock=lock)
        if machine_name is not None:
            app.state.sync_hub_machine_name = machine_name
        # The live-set gate for ENGINE exceptions (browser errors carry their
        # own flag): the page publishes its deck transport to app.state once a
        # second, and telemetry holds events local while it says a deck is
        # playing or audible. Registered whether telemetry is on or off, so
        # the probe is exercised on every boot rather than only the reporting
        # ones. Cheap: one attribute read per captured event, never per request.
        from apps.shared.telemetry import mirror_transport_live, set_live_transport_probe

        set_live_transport_probe(
            lambda: mirror_transport_live(getattr(app.state, "ui_mirror", None))
        )
        print(
            f"[OK] opendj engine {ENGINE_VERSION} boot_id={lock.boot_id} "
            f"contract_rev={app.state.contract_rev} "
            f"data_dir={cfg.data_dir} http://{cfg.host}:{cfg.port}",
            flush=True,
        )
        uvicorn.run(
            app,
            host=cfg.host,
            port=cfg.port,
            log_level=log_level,
            log_config=None,
            workers=1,
            # Bounded, so the lifespan shutdown (which stops the job runner
            # and reaps its worker groups) always gets to run. Unbounded,
            # uvicorn waits for every open request first, and a long-lived
            # stream never finishes: the shell's SIGKILL then lands first
            # and the job workers, which lead their own sessions, outlive
            # the app. The shell's grace for all of this is SHUTDOWN_GRACE
            # in apps/desktop/src-tauri/src/engine.rs.
            timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S,
        )
    finally:
        lock.release()
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
