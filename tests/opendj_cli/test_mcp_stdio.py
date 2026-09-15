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

from tests.opendj_cli.conftest import Engine


def _tool_json(result: Any) -> dict[str, Any]:
    texts = [block.text for block in result.content if block.type == "text"]
    assert texts, f"tool returned no text content: {result!r}"
    return json.loads(texts[0])


async def _with_session(engine: Engine, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "apps.opendj_cli", "--lock", str(engine.lock_path), "mcp"],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, arguments)
            return _tool_json(result)


def call_tool(engine: Engine, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return asyncio.run(_with_session(engine, tool, arguments))


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
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "apps.opendj_cli", "--lock", str(engine.lock_path), "mcp"],
    )

    async def _read_health() -> dict[str, Any]:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "app_state",
                    {"path": "/api/v1/health"},
                )
                return json.loads(result.content[0].text)

    body = asyncio.run(_read_health())
    assert body["status"] == "ok"


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
                return _tool_json(result)

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


@pytest.mark.requirement("AGENT-11")
def test_app_state_rejects_invalid_paths(engine: Engine) -> None:
    """[if] app_state path is outside /api/v1 [then] it returns invalid_path, [else stop]."""
    engine.page().start()
    for path in ("/health", "/api/v1/../health"):
        payload = call_tool(engine, "app_state", {"path": path})
        assert payload["error"] == "invalid_path"


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
