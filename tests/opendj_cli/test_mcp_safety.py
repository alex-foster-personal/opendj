"""Unit tests for opendj MCP safety rails."""

from __future__ import annotations

import pytest

from apps.opendj_cli.mcp_safety import (
    _DESTRUCTIVE_PATH_PATTERNS,
    guard_library_request,
    guard_order,
    order_starts_audible_play,
    prepend_master_mute,
    validate_api_path,
)
from apps.opendj_cli.mcp_safety import SafetyRefusal


def test_destructive_patterns_cover_delete_and_ingest() -> None:
    methods = {method for method, _ in _DESTRUCTIVE_PATH_PATTERNS}
    assert "DELETE" in methods
    assert "POST" in methods


def test_guard_order_prepends_mute_for_play() -> None:
    order = {"single": {"type": "play", "deck": 1, "playing": True}}
    assert order_starts_audible_play(order)
    rewritten, safety = guard_order(order)
    assert safety == {"prepended_master_mute": True}
    assert "sequence" in rewritten


def test_prepend_master_mute_wraps_single() -> None:
    order = {"single": {"type": "play", "deck": 1, "playing": True}}
    rewritten, prepended = prepend_master_mute(order)
    assert prepended is True
    assert rewritten["sequence"][0] == {"type": "master_mute", "muted": True}


def test_validate_api_path_rejects_traversal() -> None:
    with pytest.raises(SafetyRefusal) as error:
        validate_api_path("/api/v1/../health")
    assert error.value.code == "invalid_path"


def test_guard_library_writeback() -> None:
    with pytest.raises(SafetyRefusal) as error:
        guard_library_request("POST", "/api/v1/playlists/x/writeback/apply")
    assert error.value.code == "writeback_blocked"
