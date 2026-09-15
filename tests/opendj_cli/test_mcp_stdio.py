"""AGENT-11: real stdio MCP session against a real test engine."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Any

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import CallToolResult

from tests.opendj_cli.conftest import Engine


def _tool_payload(result: CallToolResult) -> dict[str, Any]:
    assert result.structured_content is not None
    assert isinstance(result.structured_content, dict)
    encoded = result.structured_content.get("result")
    assert not isinstance(encoded, str), "double-encoded structuredContent"
    return result.structured_content


def _stdio_params(engine: Engine) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "apps.opendj_cli", "--lock", str(engine.lock_path), "mcp"],
    )


async def _call_tool(
    engine: Engine,
    tool: str,
    arguments: dict[str, Any],
) -> CallToolResult:
    params = _stdio_params(engine)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool(tool, arguments)


def call_tool(engine: Engine, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return _tool_payload(asyncio.run(_call_tool(engine, tool, arguments)))


def call_tool_result(engine: Engine, tool: str, arguments: dict[str, Any]) -> CallToolResult:
    return asyncio.run(_call_tool(engine, tool, arguments))


@pytest.mark.requirement("AGENT-11")
def test_status_reports_lock_port(engine: Engine) -> None:
    """[if] Open DJ is running [then] status reports lock port and healthy engine, [else stop]."""
    engine.page().start()
    payload = call_tool(engine, "status", {})
    assert str(engine.port) in payload["origin"]
    assert payload["health"]["status"] == "ok"


@pytest.mark.requirement("AGENT-11")
def test_app_state_health(engine: Engine) -> None:
    """[if] engine is up [then] app_state /api/v1/health returns ok, [else stop]."""
    engine.page().start()
    payload = call_tool(engine, "app_state", {"path": "/api/v1/health"})
    assert payload["status"] == "ok"


@pytest.mark.requirement("AGENT-11")
def test_command_master_mute_returns_mirror_delta(engine: Engine) -> None:
    """[if] command orders master_mute [then] the result has a changed mirror_delta, [else stop]."""
    engine.page().start()
    payload = call_tool(
        engine,
        "command",
        {"order": {"single": {"type": "master_mute", "muted": True}}},
    )
    assert "mirror_delta" in payload
    assert payload["mirror_delta"].get("changed")


@pytest.mark.requirement("AGENT-11")
def test_status_engine_down(tmp_path: Any) -> None:
    """[if] no engine is running [then] status says engine_not_running within 5 s, [else stop]."""
    missing = tmp_path / "missing.engine.lock"
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "apps.opendj_cli", "--lock", str(missing), "mcp"],
    )

    async def _status() -> dict[str, Any]:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("status", {})
                return _tool_payload(result)

    started = time.monotonic()
    payload = asyncio.run(_status())
    assert time.monotonic() - started < 5.0
    assert payload["error"] == "engine_not_running"
    assert payload["lock_path"] == str(missing)


@pytest.mark.requirement("AGENT-11")
def test_library_writeback_blocked(engine: Engine) -> None:
    """[if] library targets writeback apply [then] it returns writeback_blocked, [else stop]."""
    engine.page().start()
    payload = call_tool(
        engine,
        "library",
        {
            "method": "POST",
            "path": "/api/v1/playlists/demo/writeback/apply",
        },
    )
    assert payload["error"] == "writeback_blocked"


@pytest.mark.requirement("AGENT-11")
def test_library_delete_blocked_without_destructive(engine: Engine) -> None:
    """[if] DELETE lacks the destructive flag [then] it returns destructive_blocked, [else stop]."""
    engine.page().start()
    payload = call_tool(
        engine,
        "library",
        {"method": "DELETE", "path": "/api/v1/playlists/demo"},
    )
    assert payload["error"] == "destructive_blocked"


@pytest.mark.requirement("AGENT-11")
def test_command_play_prepends_master_mute(engine: Engine) -> None:
    """[if] play is ordered without prior mute [then] master mute is prepended, [else stop]."""
    page = engine.page()
    page.start()
    payload = call_tool(
        engine,
        "command",
        {"order": {"single": {"type": "play", "deck": 1, "playing": True}}},
    )
    assert payload.get("safety", {}).get("prepended_master_mute") is True
    assert page.mirror["master"]["muted"] is True


@pytest.mark.requirement("AGENT-12")
def test_open_route_posts_navigate_and_waits_for_mirror(engine: Engine) -> None:
    """[if] open_route is called [then] shell navigate opens performance, [else stop]."""
    shell = engine.shell()
    shell.start()
    try:
        payload = call_tool(engine, "open_route", {"route": "/performance"})
    finally:
        shell.stop()
    assert payload["accepted"] is True
    assert payload["client_open"] is True
    assert payload["route"] == "/performance"


@pytest.mark.requirement("AGENT-12")
def test_command_auto_ensures_when_page_closed(engine: Engine) -> None:
    """[if] command runs with no page [then] auto-ensure opens performance, [else stop]."""
    shell = engine.shell()
    shell.start()
    try:
        payload = call_tool(
            engine,
            "command",
            {"order": {"single": {"type": "master_mute", "muted": True}}},
        )
    finally:
        shell.stop()
    assert "mirror_delta" in payload
    assert payload.get("error") != "no_performance_page"


@pytest.mark.requirement("AGENT-11")
def test_app_state_rejects_invalid_paths(engine: Engine) -> None:
    """[if] app_state path is outside /api/v1 [then] it returns invalid_path, [else stop]."""
    engine.page().start()
    for path in ("/health", "/api/v1/../health"):
        payload = call_tool(engine, "app_state", {"path": path})
        assert payload["error"] == "invalid_path"


@pytest.mark.requirement("AGENT-11")
def test_app_state_non_success_is_error(engine: Engine) -> None:
    """[if] app_state hits a 404 path [then] isError is true and names the status, [else stop]."""
    engine.page().start()
    result = call_tool_result(
        engine,
        "app_state",
        {"path": "/api/v1/performance/mirror"},
    )
    assert result.is_error is True
    texts = [block.text for block in result.content if block.type == "text"]
    assert any("404" in text for text in texts)


@pytest.mark.requirement("AGENT-11")
def test_tools_list_carries_annotations(engine: Engine) -> None:
    """[if] tools/list runs [then] every tool carries readOnlyHint, [else stop]."""
    engine.page().start()
    params = _stdio_params(engine)

    async def _list_tools() -> list[Any]:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                return listed.tools

    tools = asyncio.run(_list_tools())
    assert tools
    for tool in tools:
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is not None
    destructive = {tool.name: tool.annotations.destructive_hint for tool in tools}
    assert destructive["library"] is True
    assert destructive["command"] is True


@pytest.mark.requirement("AGENT-11")
def test_structured_content_is_object_not_string(engine: Engine) -> None:
    """[if] a tool returns JSON [then] structuredContent is the object, [else stop]."""
    engine.page().start()
    result = call_tool_result(engine, "status", {})
    assert result.structured_content is not None
    assert isinstance(result.structured_content, dict)
    assert "lock_path" in result.structured_content
    assert not isinstance(result.structured_content.get("result"), str)


@pytest.mark.requirement("AGENT-11")
def test_status_stale_lock_port(tmp_path: Any) -> None:
    """[if] the lock names a dead port [then] status reports not-running within 5 s, [else stop].

    Not-running is either ``engine_not_running`` or an ``UNREACHABLE`` health string.
    """
    lock_path = tmp_path / ".engine.lock"
    lock_path.write_text(
        json.dumps(
            {
                "pid": 1,
                "role": "opendj-engine",
                "host": "127.0.0.1",
                "port": 9,
            }
        ),
        encoding="utf-8",
    )
    started = time.monotonic()
    payload = call_tool(
        Engine(
            base_url="http://127.0.0.1:9",
            port=9,
            lock_path=lock_path,
        ),
        "status",
        {},
    )
    assert time.monotonic() - started < 5.0
    assert payload["error"] == "engine_not_running" or str(payload.get("health", "")).startswith(
        "UNREACHABLE"
    )
