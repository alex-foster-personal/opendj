"""The one place the CLI imports the MCP SDK.

The repo's mypy measurement runs in an isolated environment holding mypy and
nothing else (see ``ops/quality/mypy-requirements.txt``), so a third-party
import that cannot resolve there scores one error per import LINE. Two modules
importing three names each therefore cost six, and the debt grew every time a
serve-side module was split for the 600-line ceiling, which is a size
constraint deciding a type-debt number.

Re-exporting the three names here makes the count a property of the suite
rather than of how its modules happen to be divided: a new module imports
:mod:`apps.opendj_cli.mcp_sdk` and adds none.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

__all__ = ["MCPServer", "ToolAnnotations", "ToolError"]
