"""``python -m apps.engine_core serve --data-dir PATH --port N [--host H]``.

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
import sys
from collections.abc import Sequence

from apps.engine_core.config import (
    ENGINE_VERSION,
    EngineBootError,
    EngineConfig,
    apply_env_contract,
    assert_no_progress_ledger,
    assert_single_worker,
    build_config,
    prepare_layout,
)
from apps.engine_core.lock import EngineLock, EngineLockError

EXIT_OK: int = 0
EXIT_LOCKED: int = 1
EXIT_REFUSED: int = 2


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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = build_config(args.data_dir, args.host, args.port)
        _preflight(cfg, workers=args.workers)
    except EngineBootError as exc:
        print(f"[ERROR] engine refused to boot: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    return _serve(cfg, log_level=args.log_level)


def _preflight(cfg: EngineConfig, *, workers: int) -> None:
    assert_single_worker(workers)
    if not cfg.data_dir.is_dir():
        # Never invent a library root: a typo in --data-dir would otherwise
        # produce a silently empty library that looks like a real one.
        raise EngineBootError(
            f"--data-dir {cfg.data_dir} does not exist. Create it first; the "
            "engine only creates what it owns (state/, jobs.db, .engine.lock)."
        )
    assert_no_progress_ledger(cfg.data_dir)
    apply_env_contract(cfg)
    prepare_layout(cfg)


def _serve(cfg: EngineConfig, *, log_level: str) -> int:
    lock = EngineLock(cfg.lock_path)
    try:
        lock.acquire()
    except EngineLockError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return EXIT_LOCKED
    try:
        # Imported here, never at module scope: this pulls in apps.webui,
        # which reads MDT_DATA_DIR at import time.
        import uvicorn

        from apps.engine_core.app import create_app

        app = create_app(cfg, lock=lock)
        print(
            f"[OK] opendj engine {ENGINE_VERSION} boot_id={lock.boot_id} "
            f"contract_rev={app.state.contract_rev} "
            f"data_dir={cfg.data_dir} http://{cfg.host}:{cfg.port}",
            flush=True,
        )
        uvicorn.run(
            app, host=cfg.host, port=cfg.port, log_level=log_level, workers=1
        )
    finally:
        lock.release()
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
