"""MCP update tools over real stdio (AGENT-13, issue #2942).

Split out of ``tests/test_mcp_stdio.py`` so that file stays under the repo's
600-line ceiling. The tool-list annotations and the empty-HOME refusals stay
there, because those are extensions of the existing AGENT-11 coverage; these
are the new tools' own flows.

Every test drives the real ``opendj mcp`` stdio server against the updater rig
in :mod:`tests.opendj_cli.updater_rig`: a real engine, a real manifest channel
and a real relaunch. There is no mock of the updater anywhere in this file.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

import pytest
from mcp import StdioServerParameters
from mcp.client.session import ClientSession
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult

from tests.opendj_cli.mcp_support import tool_payload
from tests.opendj_cli.updater_rig import Updater

RUNNING_VERSION = "0.1.0"
RELEASED_VERSION = "0.1.1"
RUNNING_SHA = "0d41a28c0000000000000000000000000000beef"
RELEASED_SHA = "deadbeef0000000000000000000000000000beef"


def _updater_params(
    updater: Updater, *, env: dict[str, str] | None = None
) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "apps.opendj_cli", "--lock", str(updater.lock_path), "mcp"],
        env=env,
    )


def _call_updater(
    updater: Updater,
    tool: str,
    arguments: dict[str, Any],
    *,
    env: dict[str, str] | None = None,
) -> CallToolResult:
    params = _updater_params(updater, env=env)

    async def _call() -> CallToolResult:
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool(tool, arguments)

    return asyncio.run(_call())


@pytest.mark.requirement("AGENT-13")
def test_update_check_reports_the_channel(updater: Updater) -> None:
    """[if] the channel offers a newer build [then] update_check returns the
    engine's own check document, [else stop]."""
    updater.start(
        app_version=RUNNING_VERSION, git_sha_full=RUNNING_SHA, publish=RELEASED_VERSION
    )
    result = _call_updater(updater, "update_check", {})
    assert result.is_error is False
    payload = tool_payload(result)
    assert payload["status"] == "update-available"
    assert payload["current_version"] == RUNNING_VERSION
    assert payload["available_version"] == RELEASED_VERSION


@pytest.mark.requirement("AGENT-13")
def test_update_check_is_an_error_when_the_channel_cannot_be_read(
    updater: Updater, tmp_path: Any
) -> None:
    """[if] the channel is not offering anything actionable [then] update_check
    is an error result naming the status, [else stop]."""
    updater.start(
        app_version="0.2.0", git_sha_full=RUNNING_SHA, publish=RUNNING_VERSION
    )
    result = _call_updater(updater, "update_check", {})
    assert result.is_error is True
    texts = [block.text for block in result.content if block.type == "text"]
    assert any("ahead-of-channel" in text for text in texts)


@pytest.mark.requirement("AGENT-13")
def test_update_apply_is_gated_like_the_other_destructive_calls(updater: Updater) -> None:
    """[if] the destructive gate is closed [then] update_apply is an error
    naming destructive_blocked and posts no order, [else stop]."""
    updater.start(
        app_version=RUNNING_VERSION, git_sha_full=RUNNING_SHA, publish=RELEASED_VERSION
    )
    updater.start_shell(
        lambda: updater.relaunch(
            app_version=RELEASED_VERSION, git_sha_full=RELEASED_SHA
        )
    )
    env = {**os.environ, "OPENDJ_MCP_ENABLE_DESTRUCTIVE": ""}
    result = _call_updater(updater, "update_apply", {}, env=env)
    assert result.is_error is True
    texts = [block.text for block in result.content if block.type == "text"]
    assert any("destructive_blocked" in text for text in texts)
    assert updater.apply_posts == 0, "a gated call must post nothing"
    assert updater.shell_claims == 0


@pytest.mark.requirement("AGENT-13")
def test_update_apply_installs_and_reports_both_identities(updater: Updater) -> None:
    """[if] the gate is open and the channel offers a newer build [then]
    update_apply returns the before and after identities, [else stop]."""
    updater.start(
        app_version=RUNNING_VERSION, git_sha_full=RUNNING_SHA, publish=RELEASED_VERSION
    )
    updater.start_shell(
        lambda: updater.relaunch(
            app_version=RELEASED_VERSION, git_sha_full=RELEASED_SHA
        )
    )
    env = {**os.environ, "OPENDJ_MCP_ENABLE_DESTRUCTIVE": "1"}
    result = _call_updater(updater, "update_apply", {"timeout_s": 60.0}, env=env)
    updater.wait_for_shell()
    assert updater.shell_error is None
    assert result.is_error is False, result.content
    payload = tool_payload(result)
    assert payload["applied"] is True
    assert payload["before"]["app_version"] == RUNNING_VERSION
    assert payload["after"]["app_version"] == RELEASED_VERSION


@pytest.mark.requirement("AGENT-13")
def test_update_apply_is_an_error_when_the_install_does_not_land(updater: Updater) -> None:
    """[if] the shell reports the install failed [then] update_apply is an error
    carrying the shell's own error, [else stop]."""
    updater.start(
        app_version=RUNNING_VERSION, git_sha_full=RUNNING_SHA, publish=RELEASED_VERSION
    )
    updater.start_shell(
        lambda: updater.relaunch(
            app_version=RELEASED_VERSION, git_sha_full=RELEASED_SHA
        ),
        result={
            "status": "failed",
            "outcome": "refused",
            "error": "minisign verify failed",
        },
    )
    env = {**os.environ, "OPENDJ_MCP_ENABLE_DESTRUCTIVE": "1"}
    result = _call_updater(updater, "update_apply", {"timeout_s": 60.0}, env=env)
    updater.wait_for_shell()
    assert result.is_error is True
    texts = [block.text for block in result.content if block.type == "text"]
    assert any("minisign verify failed" in text for text in texts)
