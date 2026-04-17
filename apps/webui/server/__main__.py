"""CLI entry point for the webui daemon.

Runs:
  python -m apps.webui.server                           # dev uvicorn
  python -m apps.webui.server --dump-openapi out.json   # schema dump
  python -m apps.webui.server --help                    # usage

For uvicorn's auto-reload / production stage, invoke uvicorn directly:
  uvicorn apps.webui.server.app:app --host 127.0.0.1 --port 8585
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from apps.webui.server.app import app


def _dump_openapi(out_path: str) -> int:
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
    parser.add_argument("--port", type=int, default=8585)
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

    try:
        import uvicorn  # type: ignore
    except ImportError:  # pragma: no cover
        sys.stderr.write(
            "uvicorn is required to run the server. Install via "
            "`pip install uvicorn[standard]`.\n"
        )
        return 1

    if args.host != "127.0.0.1" and args.host != "localhost":
        sys.stderr.write(
            f"WARNING: binding to {args.host}. Do NOT expose the server to "
            "the public internet without Tailscale or Cloudflare Tunnel.\n"
        )

    uvicorn.run(  # pragma: no cover - io
        "apps.webui.server.app:app",
        host=args.host, port=args.port,
        reload=args.reload and not args.prod,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["main"]
