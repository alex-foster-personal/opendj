"""CLI entry point for the webui daemon.

Runs:
  python -m apps.webui.server                           # root .env port
  python -m apps.webui.server --dump-openapi out.json   # schema dump
  python -m apps.webui.server --help                    # usage

For uvicorn's auto-reload / production stage, pass an explicit port from the
same worktree configuration.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from apps.shared.sync_bind_guard import SyncBindRefused, assert_sync_bind_allowed
from apps.shared.uvicorn_shutdown import GRACEFUL_SHUTDOWN_S
from apps.webui.port_config import (
    BACKEND_ENV,
    FRONTEND_ENV,
    PortConfigError,
    check_reservation,
    claim_ports,
    resolve_backend_port,
)
from apps.webui.port_config import (
    WEBUI_ENV_FILE as DEFAULT_WEBUI_ENV_FILE,
)
from apps.webui.server.request_guard import (
    RequestGuardBindRefused,
    assert_request_guard_bind_allowed,
)

WEBUI_ENV_FILE = DEFAULT_WEBUI_ENV_FILE


def _dump_openapi(out_path: str) -> int:
    from apps.webui.server.app import app

    schema = app.openapi()
    dest = Path(out_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    sys.stdout.write(f"wrote OpenAPI schema to {dest}\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="apps.webui.server",
        description="music-dj-tools web UI daemon",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("MUSIC_DJ_BIND_HOST", "127.0.0.1"),
        help="Bind host (default: 127.0.0.1). Override via MUSIC_DJ_BIND_HOST.",
    )
    parser.add_argument(
        "--port", type=int, default=None,
        help=(
            "Bind port. Overrides MUSIC_DJ_BACKEND_PORT from the process "
            "environment or worktree root .env."
        ),
    )
    parser.add_argument("--reload", action="store_true",
                        help="Enable uvicorn auto-reload (development).")
    parser.add_argument(
        "--dump-openapi", metavar="PATH", default=None,
        help="Dump the OpenAPI schema to PATH and exit.",
    )
    parser.add_argument(
        "--prod", action="store_true",
        help="Run without --reload; CORS stays on.",
    )
    args = parser.parse_args(argv)

    if args.dump_openapi:
        return _dump_openapi(args.dump_openapi)

    # This daemon mounts /api/v1/sync/* too: same bind rule as the engine.
    try:
        assert_sync_bind_allowed(args.host)
        assert_request_guard_bind_allowed(args.host)
    except (SyncBindRefused, RequestGuardBindRefused) as exc:
        sys.stderr.write(f"[ERROR] {exc}\n")
        return 2

    from apps.shared import platform_paths
    from apps.shared.library_mode import apply_library_env, assert_ready

    apply_library_env()
    platform_paths.refresh_share_root()
    assert_ready()

    try:
        if (
            args.port is None
            and not args.prod
            and WEBUI_ENV_FILE == DEFAULT_WEBUI_ENV_FILE
            and (DEFAULT_WEBUI_ENV_FILE.parent / ".git").exists()
        ):
            claimed_ports = claim_ports()
            resolved_port = claimed_ports.backend
            os.environ[FRONTEND_ENV] = str(claimed_ports.frontend)
            check_reservation("backend")
        else:
            resolved_port = resolve_backend_port(
                args.port, dotenv_path=WEBUI_ENV_FILE,
            )
    except PortConfigError as exc:
        parser.error(str(exc))

    os.environ[BACKEND_ENV] = str(resolved_port)

    from apps.shared.process_identity import set_process_identity

    set_process_identity("Backend", resolved_port)

    from apps.webui.server.app import app

    app.state.port = resolved_port

    try:
        import uvicorn  # type: ignore
    except ImportError:  # pragma: no cover
        sys.stderr.write(
            "uvicorn is required to run the server. Install via "
            "`uv add 'uvicorn[standard]'`.\n"
        )
        return 1

    if args.host != "127.0.0.1" and args.host != "localhost":
        sys.stderr.write(
            f"WARNING: binding to {args.host}. Do NOT expose the server to "
            "the public internet without Tailscale or Cloudflare Tunnel.\n"
        )

    uvicorn.run(  # pragma: no cover - io
        "apps.webui.server.app:create_process_app",
        host=args.host, port=resolved_port,
        reload=args.reload and not args.prod,
        factory=True,
        # Bounded for the same reason apps.engine_core.__main__ bounds it: a
        # dev client's keep-alive connections (vite's proxy held 29 of them
        # during the Mon 5 Oct 2026 hot-reload hang) must not make SIGTERM
        # wait forever for "Waiting for connections to close".
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["main"]
