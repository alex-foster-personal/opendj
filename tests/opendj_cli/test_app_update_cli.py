"""Restart to update on the installed CLI and the MCP server (INSTALL-39).

Agent-native parity: the UI's "Restart to update" nudge reads
``/api/v1/app-update/status`` and its click posts ``/api/v1/app-update/install``.
Here ``opendj update ready|status|install`` and the MCP tools
``app_update_ready|status|install`` drive the SAME routes against the real
updater rig (real engine on a socket, real lock file, a shell thread that
claims the order and relaunches a second engine reporting the flagged sha).

Regression lines:
- if a UI action has no CLI verb or MCP tool then an agent cannot do what the
  nudge does
- if install exits 0 before the relaunched app reports the flagged sha then an
  agent is told a build is running that is not
- if app_update_install runs with the destructive gate closed then an agent
  can replace the app without the opt-in every other install needs
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from apps.engine_core.local_update_client import InstallOutcome, install_via_engine
from apps.opendj_cli import update_cli
from tests.engine_core.local_update_archive import BUILT_SHA, build_archive
from tests.opendj_cli import mcp_support
from tests.opendj_cli.updater_rig import Updater

VERSION = "1.0.0-alpha.1"
RUNNING_SHA = "0d41a28c0000000000000000000000000000beef"


def _run(capsys: pytest.CaptureFixture[str], updater: Updater, *tokens: str) -> tuple[int, dict[str, Any]]:
    code = update_cli.run(list(tokens), as_json=True, lock=updater.lock_path)
    return code, json.loads(capsys.readouterr().out)


def _start(updater: Updater) -> Path:
    updater.start(app_version=VERSION, git_sha_full=RUNNING_SHA, publish=VERSION)
    (updater.tmp_path / "build").mkdir()
    return build_archive(updater.tmp_path / "build")


@pytest.mark.requirement("INSTALL-39")
def test_every_nudge_action_has_a_cli_verb_and_an_mcp_tool(updater: Updater) -> None:
    """[if] the nudge reads status or installs [then] a CLI verb and an MCP tool do too, [else stop]."""
    assert {update_cli.READY_COMMAND, update_cli.STATUS_COMMAND, update_cli.INSTALL_COMMAND} <= set(update_cli._VERBS)
    updater.start(app_version=VERSION, git_sha_full=RUNNING_SHA, publish=VERSION)

    async def _list() -> dict[str, Any]:
        async with mcp_support.session(mcp_support.stdio_params(updater.lock_path)) as session:
            return {tool.name: tool.annotations for tool in (await session.list_tools()).tools}

    tools = asyncio.run(_list())
    assert tools["app_update_status"].read_only_hint is True
    assert tools["app_update_install"].destructive_hint is True
    assert tools["app_update_ready"].destructive_hint is False
    assert tools["app_update_cancel"].destructive_hint is False


@pytest.mark.requirement("INSTALL-39")
def test_cli_ready_status_install_round_trip(updater: Updater, capsys: pytest.CaptureFixture[str]) -> None:
    """[if] a build is flagged and installed [then] install exits 0 only once the flagged sha runs, [else stop]."""
    archive = _start(updater)
    code, ready = _run(capsys, updater, "ready", str(archive), "--notes", "set fix")
    assert code == 0, ready
    assert ready["state"] == "pending"
    code, status = _run(capsys, updater, "status")
    assert (code, status["pending"]["git_sha_full"]) == (0, BUILT_SHA)

    updater.start_shell(lambda: updater.relaunch(app_version=VERSION, git_sha_full=BUILT_SHA))
    code, installed = _run(capsys, updater, "install", "--timeout-s", "60")
    updater.wait_for_shell()
    assert code == 0, installed
    assert installed["installed"] is True
    assert installed["before"]["git_sha_full"] == RUNNING_SHA
    assert installed["after"]["git_sha_full"] == BUILT_SHA
    code, after = _run(capsys, updater, "status")
    assert after["state"] == "installed"


@pytest.mark.requirement("INSTALL-39")
def test_cli_refusals_carry_the_engine_code(updater: Updater, capsys: pytest.CaptureFixture[str]) -> None:
    """[if] nothing is pending or the archive is unsigned [then] the verb exits 2 naming why, [else stop]."""
    archive = _start(updater)
    code, refused = _run(capsys, updater, "install")
    assert code == 2
    assert refused["reason"] == "not-pending:none"
    archive.with_name(archive.name + ".sig").unlink()
    code, unsigned = _run(capsys, updater, "ready", str(archive))
    assert code == 2
    assert unsigned["error"]["code"] == "signature-missing"
    assert updater.shell_claims == 0


@pytest.mark.requirement("INSTALL-39")
def test_mcp_tools_drive_the_same_flow_and_install_is_gated(updater: Updater) -> None:
    """[if] the gate is closed [then] app_update_install posts nothing; open, it installs, [else stop]."""
    archive = _start(updater)
    ready = mcp_support.call_tool(updater.lock_path, "app_update_ready", {"artifact": str(archive)})
    assert ready.is_error is False, ready.content
    status = mcp_support.tool_payload(mcp_support.call_tool(updater.lock_path, "app_update_status", {}))
    assert status["state"] == "pending"

    gated = mcp_support.call_tool(updater.lock_path, "app_update_install", {}, env=mcp_support.closed_gate_env())
    assert gated.is_error is True
    assert "destructive_blocked" in mcp_support.tool_text(gated)

    updater.start_shell(lambda: updater.relaunch(app_version=VERSION, git_sha_full=BUILT_SHA))
    result = mcp_support.call_tool(
        updater.lock_path, "app_update_install", {"timeout_s": 60.0}, env=mcp_support.destructive_env()
    )
    updater.wait_for_shell()
    assert result.is_error is False, result.content
    assert mcp_support.tool_payload(result)["after"]["git_sha_full"] == BUILT_SHA


@pytest.mark.requirement("INSTALL-43")
def test_cli_cancel_withdraws_an_install_in_its_countdown(updater: Updater, capsys: pytest.CaptureFixture[str]) -> None:
    """[if] `opendj update cancel` runs in the countdown [then] install exits 2 `cancelled` and the shell gets nothing, [else stop]."""
    assert update_cli.CANCEL_COMMAND in update_cli._VERBS
    updater.countdown_s = 5.0
    archive = _start(updater)
    assert _run(capsys, updater, "ready", str(archive))[0] == 0
    code, refused = _run(capsys, updater, "cancel")
    assert (code, refused["error"]["code"]) == (2, "not-counting-down")

    results: dict[str, InstallOutcome] = {}
    installer = threading.Thread(
        target=lambda: results.update(outcome=install_via_engine(updater.base_url, 30.0, lock_path=updater.lock_path))
    )
    installer.start()
    deadline = time.monotonic() + 5.0
    while not _counting_down(updater) and time.monotonic() < deadline:
        time.sleep(0.05)
    code, cancelled = _run(capsys, updater, "cancel")
    installer.join(timeout=30)
    assert code == 0, cancelled
    assert cancelled["install"]["outcome"] == "cancelled"
    assert (results["outcome"].code, results["outcome"].reason) == (2, "cancelled")
    assert updater.shell_claims == 0


def _counting_down(updater: Updater) -> bool:
    broker = getattr(updater.apps[-1].state, "shell_command_broker", None)
    return broker is not None and bool(broker.pending)
