"""Small helpers shared by the MCP stdio test modules.

Both ``test_mcp_stdio.py`` and ``test_mcp_update_tools.py`` read the payload a
tool returned; this is the one place that knows how it is shaped, so the two
files cannot disagree about it.
"""

from __future__ import annotations

from typing import Any

from mcp.types import CallToolResult


def tool_payload(result: CallToolResult) -> dict[str, Any]:
    """The structured payload a successful tool call returned."""
    assert result.structured_content is not None
    assert isinstance(result.structured_content, dict)
    encoded = result.structured_content.get("result")
    assert not isinstance(encoded, str), "double-encoded structuredContent"
    return result.structured_content
