"""``opendj status``: MCP status parity (issue #3035)."""

from __future__ import annotations

import json

import pytest

from apps.opendj_cli import EXIT_NO_ENGINE
from apps.opendj_cli.__main__ import main
from tests.opendj_cli.conftest import Engine
from tests.opendj_cli.test_mcp_stdio import call_tool


@pytest.mark.requirement("AGENT-11")
def test_status_json_matches_mcp(engine: Engine, capsys: pytest.CaptureFixture[str]) -> None:
    """[if] status runs with --json [then] its document equals the MCP status doc, [else stop]."""
    engine.page().start()
    mcp_status = call_tool(engine, "status", {})

    assert (
        main(["--lock", str(engine.lock_path), "--json", "status"])
        == 0
    )

    captured = capsys.readouterr()
    cli_doc = json.loads(captured.out)
    assert cli_doc == mcp_status


def test_status_rejects_extra_args(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["status", "extra"]) != 0
    assert capsys.readouterr().out == ""


def test_status_engine_down(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    missing = tmp_path / "missing.engine.lock"

    assert main(["--lock", str(missing), "status"]) == EXIT_NO_ENGINE

    captured = capsys.readouterr()
    assert str(missing) in captured.err
