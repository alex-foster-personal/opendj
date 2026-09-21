"""The one place this package imports the MCP SDK.

Same reason as :mod:`apps.opendj_cli.mcp_sdk`: the repo's mypy measurement
runs in an isolated environment holding mypy and nothing else, so a
third-party import that cannot resolve there costs one error per import LINE.
Re-exporting here keeps that count a property of the package rather than of
how its modules happen to be split.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

__all__ = ["MCPServer", "ToolAnnotations", "ToolError"]
