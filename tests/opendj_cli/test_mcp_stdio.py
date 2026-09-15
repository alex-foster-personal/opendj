"""AGENT-11: real stdio MCP session against a real test engine."""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

import pytest

from tests.opendj_cli import mcp_support
from tests.opendj_cli.conftest import Engine
from tests.opendj_cli.mcp_support import tool_payload


def _stdio_params(engine: Engine) -> Any:
    return mcp_support.stdio_params(engine.lock_path)


async def _call_tool(engine: Engine, tool: str, arguments: dict[str, Any]) -> Any:
    return await mcp_support.call_tool_async(engine.lock_path, tool, arguments)


def call_tool(engine: Engine, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return tool_payload(asyncio.run(_call_tool(engine, tool, arguments)))


def call_tool_result(engine: Engine, tool: str, arguments: dict[str, Any]) -> Any:
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

    async def _status() -> Any:
        return await mcp_support.call_tool_async(missing, "status", {})

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
    """[if] tools/list is called [then] every tool carries annotations, [else stop].
    [if] tools/list runs [then] every tool carries readOnlyHint, and the
    calls that change the world carry destructiveHint, [else stop]."""
    engine.page().start()
    async def _list_tools() -> list[Any]:
        async with mcp_support.session(_stdio_params(engine)) as session:
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
        payload = tool_payload(result)
        assert str(payload.get("health", "")).startswith("UNREACHABLE")


@pytest.mark.requirement("AGENT-11")
def test_home_empty_tmp_dir_is_error_for_every_tool(tmp_path: Any) -> None:
    """[if] HOME is an empty tmp dir [then] every tool answers an error, [else stop].
    [if] HOME has no engine lock [then] every tool surfaces isError True with
    its error code readable in the text, [else stop].

    Issue #2895 acceptance: drive real stdio, no mocking. HOME points at an
    empty temp dir so ``resolve_origin`` naturally finds no lock file. The
    destructive gate is closed explicitly rather than inherited, so ``library``
    and ``update_apply`` are refused deterministically by the rail whose code
    the case names, on a machine where nothing has been enabled.
    """
    env = mcp_support.naive_home_env(tmp_path)

    async def _call(tool: str, arguments: dict[str, Any]) -> Any:
        return await mcp_support.call_tool_async(None, tool, arguments, env=env)

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
    """[if] library GETs /tracks with no limit [then] the page stays under the MCP budget, [else stop].
    [if] library GETs /tracks with no limit against an 8500-track library
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
    """[if] library GETs /tracks over the limit [then] it refuses with a readable message, [else stop].
    [if] library GETs /tracks?limit=100000 [then] the engine still refuses
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
    """[if] library GETs /tracks with fields [then] rows carry only those fields, [else stop].
    [if] library GETs /tracks?fields=stable_id,title,artist,bpm,key [then]
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
    """[if] a bounded page returns a cursor [then] the next call pages with it, [else stop].
    [if] the auto-limited page's next_cursor is paged again [then] it
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


@pytest.mark.requirement("AGENT-05")
def test_status_boot_id_mismatch_is_error(engine: Engine, tmp_path: Path) -> None:
    """[if] lock boot_id disagrees with health [then] status is engine_identity_mismatch."""
    bad_lock = tmp_path / "bad.lock"
    bad_lock.write_text(
        json.dumps(
            {
                "pid": 4242,
                "role": "opendj-engine",
                "host": "127.0.0.1",
                "port": engine.port,
                "boot_id": "wrong-boot",
            }
        ),
        encoding="utf-8",
    )
    result = asyncio.run(mcp_support.call_tool_async(bad_lock, "status", {}))
    assert result.is_error is True
    texts = [block.text for block in result.content if block.type == "text"]
    assert any('"error": "engine_identity_mismatch"' in text for text in texts)


@pytest.mark.requirement("AGENT-05")
def test_command_wrong_role_is_error(engine: Engine, tmp_path: Path) -> None:
    """[if] lock role is not opendj-engine [then] command is engine_identity_mismatch."""
    bad_lock = tmp_path / "bad.lock"
    bad_lock.write_text(
        json.dumps(
            {
                "pid": 4242,
                "role": "not-opendj-engine",
                "host": "127.0.0.1",
                "port": engine.port,
                "boot_id": "test-boot",
            }
        ),
        encoding="utf-8",
    )
    result = asyncio.run(
        mcp_support.call_tool_async(
            bad_lock,
            "command",
            {"order": {"single": {"type": "master_mute", "muted": True}}},
        )
    )
    assert result.is_error is True
    texts = [block.text for block in result.content if block.type == "text"]
    assert any('"error": "engine_identity_mismatch"' in text for text in texts)


@pytest.mark.requirement("AGENT-05")
def test_command_matching_engine_succeeds(engine: Engine) -> None:
    """[if] lock matches the live engine [then] command still dispatches."""
    engine.page().start()
    payload = call_tool(
        engine,
        "command",
        {"order": {"single": {"type": "master_mute", "muted": True}}},
    )
    assert "mirror_delta" in payload
