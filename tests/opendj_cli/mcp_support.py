"""The one place the MCP stdio test modules import the client SDK.

Every stdio test drives the real ``opendj mcp`` server, and the repo's mypy
measurement runs in an isolated environment where that SDK has no stubs, so
each module that imports it scores one error per import line. Centralising the
imports here keeps the count a property of the suite rather than of how its
files happen to be split: a new test module imports these helpers and adds
none.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip(
    "mcp",
    reason=(
        "UNAVAILABLE: the MCP client SDK is not installed here, so the stdio "
        "session tests cannot run. This is a capability report, not a pass."
    ),
)

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import CallToolResult


def stdio_params(
    lock_path: Path | None, *, env: dict[str, str] | None = None
) -> StdioServerParameters:
    """Params for one ``opendj mcp`` server, over the lock file given."""
    args = ["-m", "apps.opendj_cli"]
    if lock_path is not None:
        args += ["--lock", str(lock_path)]
    # --lock is a global flag, so it precedes the subcommand.
    return StdioServerParameters(
        command=sys.executable, args=[*args, "mcp"], env=env
    )


def module_stdio_params(
    module: str, *args: str, env: dict[str, str] | None = None
) -> StdioServerParameters:
    """Params for any ``python -m <module>`` stdio MCP server in this repo.

    The generic sibling of :func:`stdio_params`, which is opendj-specific.
    Here for the same reason the rest of this module is: a test that built its
    own params would have to import the client SDK itself, and the repo's mypy
    measurement scores one error per such import line.
    """
    return StdioServerParameters(command=sys.executable, args=["-m", module, *args], env=env)


@asynccontextmanager
async def session(params: StdioServerParameters) -> AsyncIterator[ClientSession]:
    """An initialized session against ``params``, for calls ``call_tool`` cannot make."""
    async with stdio_client(params) as (read, write), ClientSession(read, write) as s:
        await s.initialize()
        yield s


async def call_tool_async(
    lock_path: Path | None,
    tool: str,
    arguments: dict[str, Any],
    *,
    env: dict[str, str] | None = None,
) -> CallToolResult:
    """Call ``tool`` once, over a fresh server process."""
    async with session(stdio_params(lock_path, env=env)) as s:
        return await s.call_tool(tool, arguments)


def call_tool(
    lock_path: Path | None,
    tool: str,
    arguments: dict[str, Any],
    *,
    env: dict[str, str] | None = None,
) -> CallToolResult:
    """Blocking form of :func:`call_tool_async`."""
    return asyncio.run(call_tool_async(lock_path, tool, arguments, env=env))


def tool_payload(result: CallToolResult) -> dict[str, Any]:
    """The structured payload a successful tool call returned."""
    assert result.structured_content is not None
    assert isinstance(result.structured_content, dict)
    encoded = result.structured_content.get("result")
    assert not isinstance(encoded, str), "double-encoded structuredContent"
    return result.structured_content


def destructive_env() -> dict[str, str]:
    """The environment that opens the destructive gate: armed, plus this process's."""
    return {**os.environ, "OPENDJ_MCP_ENABLE_DESTRUCTIVE": "1"}


def closed_gate_env() -> dict[str, str]:
    """This process's environment with the destructive gate explicitly closed.

    Stated rather than inherited: the gate must be shut because the test says
    so, not because the machine running it happens to have nothing enabled.
    """
    return {**os.environ, "OPENDJ_MCP_ENABLE_DESTRUCTIVE": ""}


def naive_home_env(home: Path) -> dict[str, str]:
    """A HOME with no engine lock, and the destructive gate explicitly closed."""
    return {**closed_gate_env(), "HOME": str(home)}
