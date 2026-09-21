"""A real stdio MCP session against the dispatch server (AGENT-15).

Everything else in this package tests the Python underneath. This test is the
only one that proves the thing an agent actually gets: a process that speaks
MCP on stdin and stdout, lists its tools, and answers a call.

The client SDK is reached through :mod:`tests.opendj_cli.mcp_support`, which
exists to be the one import point for it (the repo's mypy measurement scores
one error per direct import line), so this module adds none.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from tests.opendj_cli import mcp_support


async def _session_call(tool: str, arguments: dict[str, Any]) -> tuple[list[str], Any]:
    params = mcp_support.module_stdio_params("apps.fleet_mcp")
    async with mcp_support.session(params) as session:
        listed = await session.list_tools()
        result = await session.call_tool(tool, arguments)
    payload = json.loads(result.content[0].text) if result.content else None
    return [entry.name for entry in listed.tools], payload


@pytest.mark.requirement("AGENT-15")
def test_a_real_stdio_session_lists_tools_and_answers_playbook():
    """[if] an agent starts the dispatch server [then] it lists tools, [else stop]"""
    names, payload = asyncio.run(_session_call("playbook", {}))
    assert "playbook" in names
    assert "queue_add" in names
    assert payload["queue_repo"] == "maintainer/music-dj-tools"
    assert "run an arbitrary command on nucbox" in payload["withheld"]
