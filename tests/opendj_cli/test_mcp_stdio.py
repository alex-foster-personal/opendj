"""AGENT-11: real stdio MCP session against a real test engine."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import CallToolResult

from tests.opendj_cli.conftest import Engine, Updater

RUNNING_VERSION = "0.1.0"
RELEASED_VERSION = "0.1.1"
RUNNING_SHA = "0d41a28c0000000000000000000000000000beef"
RELEASED_SHA = "deadbeef0000000000000000000000000000beef"


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


@pytest.mark.requirement("UXR-01")
def test_command_master_mute_persists_to_ui_prefs(engine: Engine, tmp_path: Path) -> None:
    """[if] MCP command master_mute [then] app_state ui-prefs shows master_muted, [else stop]."""
    engine.app.state.data_dir = tmp_path / "data"
    engine.page().start()
    call_tool(
        engine,
        "command",
        {"order": {"single": {"type": "master_mute", "muted": True}}},
    )
    payload = call_tool(engine, "app_state", {"path": "/api/v1/ui-prefs"})
    assert payload["master_muted"] is True


@pytest.mark.requirement("AGENT-11")
def test_status_engine_down(tmp_path: Any) -> None:
    """[if] no engine is running [then] status is isError with engine_not_running, [else stop]."""
    missing = tmp_path / "missing.engine.lock"
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "apps.opendj_cli", "--lock", str(missing), "mcp"],
    )

    async def _status() -> CallToolResult:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool("status", {})

    started = time.monotonic()
    result = asyncio.run(_status())
    assert time.monotonic() - started < 5.0
    assert result.is_error is True
    texts = [block.text for block in result.content if block.type == "text"]
    assert any("engine_not_running" in text for text in texts)
    assert any(str(missing) in text for text in texts)


@pytest.mark.requirement("AGENT-05")
def test_library_ui_prefs_topbar_round_trip(engine: Engine) -> None:
    """[if] MCP library PUTs auto_play_enforce_order [then] GET returns it, [else stop]."""
    put = call_tool(
        engine,
        "library",
        {
            "method": "PUT",
            "path": "/api/v1/ui-prefs",
            "fields": {"auto_play_enforce_order": "true"},
        },
    )
    assert put["status_code"] == 200
    get = call_tool(
        engine,
        "library",
        {"method": "GET", "path": "/api/v1/ui-prefs"},
    )
    assert get["status_code"] == 200
    assert get["body"]["auto_play_enforce_order"] is True


@pytest.mark.requirement("AGENT-05")
def test_library_ui_prefs_library_browser_round_trip(engine: Engine) -> None:
    """[if] MCP library PUTs remixes_filter [then] GET returns it, [else stop]."""
    put = call_tool(
        engine,
        "library",
        {
            "method": "PUT",
            "path": "/api/v1/ui-prefs",
            "fields": {"remixes_filter": "true"},
        },
    )
    assert put["status_code"] == 200
    get = call_tool(
        engine,
        "library",
        {"method": "GET", "path": "/api/v1/ui-prefs"},
    )
    assert get["status_code"] == 200
    assert get["body"]["remixes_filter"] is True


@pytest.mark.requirement("AGENT-11")
def test_library_writeback_blocked(engine: Engine) -> None:
    """[if] library targets writeback apply [then] isError names writeback_blocked, [else stop]."""
    engine.page().start()
    result = call_tool_result(
        engine,
        "library",
        {
            "method": "POST",
            "path": "/api/v1/playlists/demo/writeback/apply",
        },
    )
    assert result.is_error is True
    texts = [block.text for block in result.content if block.type == "text"]
    assert any("writeback_blocked" in text for text in texts)


@pytest.mark.requirement("AGENT-11")
def test_library_delete_blocked_without_destructive(engine: Engine) -> None:
    """[if] DELETE lacks the destructive flag [then] isError names destructive_blocked, [else stop]."""
    engine.page().start()
    result = call_tool_result(
        engine,
        "library",
        {"method": "DELETE", "path": "/api/v1/playlists/demo"},
    )
    assert result.is_error is True
    texts = [block.text for block in result.content if block.type == "text"]
    assert any("destructive_blocked" in text for text in texts)


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
    """[if] app_state path is outside /api/v1 [then] isError names invalid_path, [else stop]."""
    engine.page().start()
    for path in ("/health", "/api/v1/../health"):
        result = call_tool_result(engine, "app_state", {"path": path})
        assert result.is_error is True
        texts = [block.text for block in result.content if block.type == "text"]
        assert any("invalid_path" in text for text in texts)


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
    """[if] tools/list runs [then] every tool carries readOnlyHint, and the
    calls that change the world carry destructiveHint, [else stop]."""
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
    by_name = {tool.name: tool.annotations for tool in tools}
    destructive = {name: annotation.destructive_hint for name, annotation in by_name.items()}
    assert destructive["library"] is True
    assert destructive["command"] is True
    # AGENT-13: the updater is on this surface too, and apply is destructive.
    assert destructive["update_apply"] is True
    assert by_name["update_check"].read_only_hint is True
    # An unset hint is not a destructive claim; only a True one would be.
    assert destructive["update_check"] is not True


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

    Not-running is either an isError ``engine_not_running``, or a success whose
    ``health`` field is an ``UNREACHABLE`` string (the port answered, just not
    with the engine's API; see the "UNREACHABLE" note in the PR body).
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
    result = call_tool_result(
        Engine(
            base_url="http://127.0.0.1:9",
            port=9,
            lock_path=lock_path,
        ),
        "status",
        {},
    )
    assert time.monotonic() - started < 5.0
    if result.is_error:
        texts = [block.text for block in result.content if block.type == "text"]
        assert any("engine_not_running" in text for text in texts)
    else:
        payload = _tool_payload(result)
        assert str(payload.get("health", "")).startswith("UNREACHABLE")


@pytest.mark.requirement("AGENT-11")
def test_home_empty_tmp_dir_is_error_for_every_tool(tmp_path: Any) -> None:
    """[if] HOME has no engine lock [then] every tool surfaces isError True with
    its error code readable in the text, [else stop].

    Issue #2895 acceptance: drive real stdio, no mocking. HOME points at an
    empty temp dir so ``resolve_origin`` naturally finds no lock file. The
    destructive gate is closed explicitly rather than inherited, so ``library``
    and ``update_apply`` are refused deterministically by the rail whose code
    the case names, on a machine where nothing has been enabled.
    """
    env = {**os.environ, "HOME": str(tmp_path), "OPENDJ_MCP_ENABLE_DESTRUCTIVE": ""}
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "apps.opendj_cli", "mcp"],
        env=env,
    )

    async def _call(tool: str, arguments: dict[str, Any]) -> CallToolResult:
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool(tool, arguments)

    cases: list[tuple[str, dict[str, Any], str]] = [
        ("status", {}, "engine_not_running"),
        ("command", {"verb": "play", "args": ["1"]}, "engine_not_running"),
        ("library", {"method": "FROB", "path": "/x"}, '"error": "usage"'),
        ("open_route", {"route": "/settings"}, "engine_not_running"),
        ("update_check", {}, "engine_not_running"),
        ("update_apply", {}, "destructive_blocked"),
    ]
    for tool, arguments, expected_code in cases:
        result = asyncio.run(_call(tool, arguments))
        assert result.is_error is True, f"{tool} {arguments} should be isError"
        texts = [block.text for block in result.content if block.type == "text"]
        assert any(expected_code in text for text in texts), (tool, texts)


@pytest.mark.requirement("AGENT-11")
def test_command_safety_refusal_is_error(engine: Engine) -> None:
    """[if] command runs with neither order nor verb [then] isError names usage (a SafetyRefusal), [else stop]."""
    engine.page().start()
    result = call_tool_result(engine, "command", {})
    assert result.is_error is True
    texts = [block.text for block in result.content if block.type == "text"]
    assert any('"error": "usage"' in text for text in texts)


@pytest.mark.requirement("AGENT-11")
def test_command_order_failed_is_error(engine: Engine) -> None:
    """[if] the engine route rejects a malformed order body [then] isError names order_failed, [else stop]."""
    engine.page().start()
    result = call_tool_result(engine, "command", {"order": {"sequence": []}})
    assert result.is_error is True
    texts = [block.text for block in result.content if block.type == "text"]
    assert any('"error": "order_failed"' in text for text in texts)


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
    payload = _tool_payload(result)
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
    payload = _tool_payload(result)
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


@pytest.mark.requirement("AGENT-11")
def test_no_unconverted_error_document_returns() -> None:
    """[if] mcp_server.py is scanned for a bare error-document return [then] none remain, proven by a control literal that DOES trip the pattern, [else stop].

    Guards the class fix in issue #2895: the next error path added to
    mcp_server.py cannot silently regress to isError:false.
    """
    source_path = (
        Path(__file__).resolve().parents[2] / "apps" / "opendj_cli" / "mcp_server.py"
    )
    pattern = re.compile(r'return\s*(_engine_not_running\(|\{"error":)')
    control = 'return {"error": "control_only", "message": "trip the pattern"}'
    assert pattern.search(control), "pattern must fire on a known-bad literal"
    source = source_path.read_text(encoding="utf-8")
    matches = pattern.findall(source)
    assert matches == [], f"unconverted error-document return(s) in mcp_server.py: {matches}"


@pytest.mark.requirement("AGENT-11")
def test_library_get_tracks_default_page_is_bounded(big_library_engine: Engine) -> None:
    """[if] library GETs /tracks with no limit against an 8500-track library
    [then] the wire result stays comfortably under the 25k-token MCP budget
    and says how to page the rest, [else stop]."""
    result = call_tool_result(
        big_library_engine, "library", {"method": "GET", "path": "/api/v1/tracks"}
    )
    assert result.is_error is not True
    texts = [block.text for block in result.content if block.type == "text"]
    estimated_tokens = sum(len(t) for t in texts) // 4
    assert estimated_tokens < 22_000, (
        f"library GET /tracks default page was ~{estimated_tokens} tokens, "
        "not comfortably under the 25k-token MCP budget"
    )

    payload = result.structured_content
    assert isinstance(payload, dict)
    body = payload["body"]
    assert isinstance(body, dict), "body must be an embedded object, not a re-encoded string"
    assert isinstance(body["items"], list)
    assert len(body["items"]) >= 20, (
        f"bounded page returned only {len(body['items'])} items; a page this thin "
        "(near the auto-shrink floor) technically satisfies '> 0' but is not a "
        "usably-sized page for an agent reading the library"
    )
    assert body["next_cursor"], "a bounded page short of an 8500-track library must still page"
    assert "cursor" in payload.get("mcp_note", ""), "must say how to get the rest"


@pytest.mark.requirement("AGENT-11")
def test_library_get_tracks_over_limit_still_refuses_readably(big_library_engine: Engine) -> None:
    """[if] library GETs /tracks?limit=100000 [then] the engine still refuses
    (422) and the body is a readable object, not an escaped JSON string,
    [else stop]."""
    payload = call_tool(
        big_library_engine,
        "library",
        {"method": "GET", "path": "/api/v1/tracks?limit=100000"},
    )
    assert payload["status_code"] == 422
    assert isinstance(payload["body"], dict), "422 body must not be a re-encoded string"


@pytest.mark.requirement("AGENT-11")
def test_library_get_tracks_fields_projects_compact_rows(big_library_engine: Engine) -> None:
    """[if] library GETs /tracks?fields=stable_id,title,artist,bpm,key [then]
    every row is projected to just those keys, [else stop]."""
    payload = call_tool(
        big_library_engine,
        "library",
        {
            "method": "GET",
            "path": "/api/v1/tracks?limit=5&fields=stable_id,title,artist,bpm,key",
        },
    )
    assert payload["status_code"] == 200
    items = payload["body"]["items"]
    assert len(items) == 5
    for item in items:
        assert set(item.keys()) == {"stable_id", "title", "artist", "bpm", "key"}


@pytest.mark.requirement("AGENT-11")
def test_library_get_tracks_bounded_page_pages_with_a_real_cursor(
    big_library_engine: Engine,
) -> None:
    """[if] the auto-limited page's next_cursor is paged again [then] it
    returns fresh, distinct tracks: a real engine page, never a client-side
    truncated slice of the first, [else stop]."""
    first = call_tool(big_library_engine, "library", {"method": "GET", "path": "/api/v1/tracks"})
    first_ids = {item["stable_id"] for item in first["body"]["items"]}
    cursor = first["body"]["next_cursor"]
    assert cursor

    second = call_tool(
        big_library_engine,
        "library",
        {"method": "GET", "path": f"/api/v1/tracks?cursor={cursor}"},
    )
    second_ids = {item["stable_id"] for item in second["body"]["items"]}
    assert second_ids, "the next page must not be empty"
    assert first_ids.isdisjoint(second_ids)
