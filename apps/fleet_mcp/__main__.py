"""``python -m apps.fleet_mcp``: the ``dispatch`` MCP server.

Default is stdio, which is what `.mcp.json` and the Codex registration launch
and needs no configuration. ``--http`` serves the remote transport for
Cloudflare Access on agentbox; it binds loopback only and refuses to start
without an Access configuration.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m apps.fleet_mcp")
    parser.add_argument(
        "--http",
        action="store_true",
        help="serve the remote streamable-HTTP transport instead of stdio",
    )
    parser.add_argument("--host", default=None, help="loopback bind address (--http only)")
    parser.add_argument("--port", type=int, default=None, help="bind port (--http only)")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if not args.http:
        if args.host is not None or args.port is not None:
            parser.error("--host and --port apply to --http only")
        from apps.fleet_mcp.server import run_stdio

        run_stdio()
        return 0

    from apps.fleet_mcp.access import AccessNotConfigured
    from apps.fleet_mcp.remote import DEFAULT_HOST, DEFAULT_PORT, BindRefused, serve_http

    try:
        serve_http(args.host or DEFAULT_HOST, args.port or DEFAULT_PORT)
    except (AccessNotConfigured, BindRefused) as error:
        # Fail loud and stay down: a remote endpoint that starts degraded is
        # worse than one that does not start.
        print(f"[dispatch-mcp] refusing to serve: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
