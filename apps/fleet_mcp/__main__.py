"""``python -m apps.fleet_mcp``: stdio entry for the ``dispatch`` MCP server."""

from __future__ import annotations

from apps.fleet_mcp.server import run_stdio

if __name__ == "__main__":
    run_stdio()
