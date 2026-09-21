"""The dispatch MCP server's own surface (AGENT-15, AGENT-16).

These tests assert the PRESENCE of each tool and of each withheld surface's
explanation. A suite that only checked "nothing raised" would pass for a
server that registered no tools at all.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from apps.fleet_mcp import SERVER_NAME
from apps.fleet_mcp.server import WITHHELD, create_server

REPO_ROOT = Path(__file__).resolve().parents[2]

EXPECTED_TOOLS = {
    "playbook",
    "queue_list",
    "queue_item",
    "queue_add",
    "queue_comment",
    "fleet_health",
    "dispatcher_log",
    "ledger_read",
    "ledger_claim",
}

READ_ONLY_TOOLS = {
    "playbook",
    "queue_list",
    "queue_item",
    "fleet_health",
    "dispatcher_log",
    "ledger_read",
}


def _tools():
    return {tool.name: tool for tool in asyncio.run(create_server().list_tools())}


@pytest.mark.requirement("AGENT-15")
def test_every_documented_tool_is_registered():
    """[if] an agent lists this server's tools [then] all nine are there, [else stop]"""
    assert set(_tools()) == EXPECTED_TOOLS


@pytest.mark.requirement("AGENT-15")
def test_every_tool_carries_a_description():
    """[if] a tool is registered [then] it explains itself to the calling agent, [else stop]"""
    undocumented = [name for name, tool in _tools().items() if not (tool.description or "").strip()]
    assert not undocumented, f"tools with no description: {undocumented}"


@pytest.mark.requirement("AGENT-16")
def test_read_and_write_tools_are_annotated_apart():
    """[if] a tool changes fleet state [then] it is not annotated read-only, [else stop]"""
    tools = _tools()
    for name in READ_ONLY_TOOLS:
        assert tools[name].annotations.read_only_hint is True, name
    for name in EXPECTED_TOOLS - READ_ONLY_TOOLS:
        assert tools[name].annotations.read_only_hint is False, name


@pytest.mark.requirement("AGENT-16")
def test_no_arbitrary_execution_tool_is_registered():
    """[if] a surface would be a remote shell or an ungated gate [then] it is absent, [else stop]

    Named explicitly rather than left to inspection: the risk is that one gets
    added later because nothing said it must not be.
    """
    forbidden = {"run", "exec", "shell", "ssh", "spawn_worker", "merge_pr", "denylist"}
    assert not forbidden.intersection(_tools())


@pytest.mark.requirement("AGENT-16")
def test_withheld_surfaces_each_name_their_real_owner():
    """[if] a surface is withheld [then] playbook says where it actually lives, [else stop]"""
    assert set(WITHHELD) >= {"spawn a worker", "merge a PR", "run an arbitrary command on nucbox"}
    for surface, owner in WITHHELD.items():
        assert owner.strip(), surface


@pytest.mark.requirement("AGENT-15")
def test_project_mcp_config_registers_this_server_by_its_launcher():
    """[if] an agent opens this checkout [then] .mcp.json already points at the shim, [else stop]"""
    config = json.loads((REPO_ROOT / ".mcp.json").read_text())
    entry = config["mcpServers"][SERVER_NAME]
    launcher = REPO_ROOT / entry["command"]
    assert launcher.is_file(), entry["command"]
    assert launcher.stat().st_mode & 0o111, f"{entry['command']} is not executable"


@pytest.mark.requirement("AGENT-15")
def test_the_launcher_pins_uv_to_this_checkout():
    """[if] the launcher runs from any cwd [then] it binds this repo's environment, [else stop]

    `uv run` with no --project walks up to the nearest ancestor pyproject.toml
    and binds ITS .venv (CLAUDE.md). The shim exists to stop that.
    """
    script = (REPO_ROOT / "scripts" / "dispatch_mcp.sh").read_text()
    assert "--project" in script
    assert "BASH_SOURCE" in script
