"""A real stdio MCP session against the dispatch server (AGENT-15).

Everything else in this package tests the Python underneath. This test is the
only one that proves the thing an agent actually gets: a process that speaks
MCP on stdin and stdout, lists its tools, and answers a call.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

import pytest

pytest.importorskip(
    "mcp",
    reason=(
        "UNAVAILABLE: the MCP client SDK is not installed here, so the stdio "
        "session test cannot run. This is a capability report, not a pass."
    ),
)

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

_PARAMS = StdioServerParameters(command=sys.executable, args=["-m", "apps.fleet_mcp"])


async def _session_call(tool: str, arguments: dict[str, Any]) -> tuple[list[str], Any]:
    async with stdio_client(_PARAMS) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        listed = await session.list_tools()
        result = await session.call_tool(tool, arguments)
    payload = json.loads(result.content[0].text) if result.content else None
    return [tool.name for tool in listed.tools], payload


@pytest.mark.requirement("AGENT-15")
def test_a_real_stdio_session_lists_tools_and_answers_playbook():
    """[if] an agent starts the dispatch server [then] it lists tools, [else stop]"""
    names, payload = asyncio.run(_session_call("playbook", {}))
    assert "playbook" in names
    assert "queue_add" in names
    assert payload["queue_repo"] == "maintainer/music-dj-tools"
    assert "run an arbitrary command on nucbox" in payload["withheld"]
