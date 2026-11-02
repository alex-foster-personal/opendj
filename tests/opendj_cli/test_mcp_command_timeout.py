"""MCP global ``--timeout`` plumbing (issue #7713)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from apps.opendj_cli import mcp_cli
from apps.opendj_cli.__main__ import main
from apps.opendj_cli.client import ORDER_TIMEOUT_S


@pytest.mark.requirement("AGENT-11")
def test_mcp_cli_forwards_command_timeout_to_stdio() -> None:
    """[if] mcp_cli.run gets command_timeout_s [then] run_stdio receives it, [else stop]."""
    captured: dict[str, float] = {}

    def _fake_run_stdio(**kwargs: object) -> None:
        captured["command_timeout_s"] = float(kwargs["command_timeout_s"])  # type: ignore[arg-type]

    with patch("apps.opendj_cli.mcp_server.run_stdio", side_effect=_fake_run_stdio):
        assert mcp_cli.run([], lock=Path("/tmp/none.lock"), command_timeout_s=17.5) == 0
    assert captured["command_timeout_s"] == 17.5


@pytest.mark.requirement("AGENT-11")
def test_main_forwards_global_timeout_to_mcp_cli() -> None:
    """[if] opendj --timeout SECS mcp [then] mcp_cli.run gets SECS, [else stop]."""
    captured: dict[str, float] = {}

    def _fake_run(*_args: object, **kwargs: object) -> int:
        captured["command_timeout_s"] = float(kwargs["command_timeout_s"])  # type: ignore[arg-type]
        return 0

    with patch("apps.opendj_cli.mcp_cli.run", side_effect=_fake_run):
        assert main(["--timeout", "22", "mcp"]) == 0
    assert captured["command_timeout_s"] == 22.0


@pytest.mark.requirement("AGENT-11")
def test_mcp_cli_default_command_timeout_matches_order_default() -> None:
    """[if] mcp_cli.run omits timeout [then] default matches ORDER_TIMEOUT_S, [else stop]."""
    captured: dict[str, float] = {}

    def _fake_run_stdio(**kwargs: object) -> None:
        captured["command_timeout_s"] = float(kwargs["command_timeout_s"])  # type: ignore[arg-type]

    with patch("apps.opendj_cli.mcp_server.run_stdio", side_effect=_fake_run_stdio):
        mcp_cli.run([])
    assert captured["command_timeout_s"] == ORDER_TIMEOUT_S
