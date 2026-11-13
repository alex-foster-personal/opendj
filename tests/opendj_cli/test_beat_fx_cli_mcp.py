"""Beat FX agent parity on the CLI and MCP surfaces (issue #9783, spec fx-fanout-20261108/01 section 4.4).

[if] the CLI or the MCP command tool lacks beat_fx or beat_fx_release [then] agents cannot drive the unit, [else stop].
"""

from __future__ import annotations

import asyncio

import pytest

from apps.opendj_cli.catalog import VERBS
from apps.opendj_cli.mcp_server import create_server
from apps.opendj_cli.verbs import describe_verbs, parse_invocation

pytestmark = pytest.mark.requirement("BEATFX-13923")


def test_beat_fx_and_beat_fx_release_build_their_bus_commands_with_help_text() -> None:
    """[if] opendj beat_fx --target 2 --enabled true or beat_fx_release --held true is missing or unhelped [then] CLI parity broke, [else stop]."""

    assert parse_invocation(["beat_fx", "--target", "2", "--enabled", "true"]).command == {
        "type": "beat_fx",
        "target": "2",
        "enabled": True,
    }
    assert parse_invocation(["beat_fx_release", "--held", "true"]).command == {"type": "beat_fx_release", "held": True}
    listed = {entry["verb"]: entry for entry in describe_verbs()}
    for name in ("beat_fx", "beat_fx_release"):
        assert name in listed, name
        assert "fx.beat_rack is off" in VERBS[name].note, name
        assert "Rust engine mode" in VERBS[name].note, name


def test_the_mcp_command_tool_names_beat_fx_and_beat_fx_release() -> None:
    """[if] the MCP command tool does not name beat_fx and beat_fx_release [then] an MCP agent cannot find them, [else stop]."""

    tools = {tool.name: tool for tool in asyncio.run(create_server().list_tools())}
    description = tools["command"].description or ""
    assert "``beat_fx``" in description
    assert "``beat_fx_release``" in description
