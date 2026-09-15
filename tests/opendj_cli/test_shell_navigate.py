"""CLI shell navigation (issue #2866, AGENT-12)."""
from __future__ import annotations

import json
import time

import pytest

from apps.opendj_cli import EXIT_CONFIRMED, EXIT_NO_PAGE
from apps.opendj_cli.__main__ import main
from apps.opendj_cli.shell_navigate import REMEDY_VERB
from tests.opendj_cli.conftest import Engine
from tests.opendj_cli.test_cli_end_to_end import _argv


@pytest.mark.requirement("AGENT-12")
def test_open_performance_moves_shell_and_reports_client_open(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] the shell is running [then] open performance reports client_open and route, [else stop]."""
    shell = engine.shell()
    shell.start()
    try:
        assert main(_argv(engine, "--json", "open", "performance")) == EXIT_CONFIRMED
    finally:
        shell.stop()

    body = json.loads(capsys.readouterr().out)
    assert body == {"client_open": True, "route": "/performance"}


@pytest.mark.requirement("AGENT-12")
def test_state_auto_ensures_performance_on_cold_launch(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] no page is open on cold launch [then] state auto-opens performance within 10s, [else stop]."""
    shell = engine.shell()
    shell.start()
    try:
        started = time.monotonic()
        assert main(_argv(engine, "--json", "state")) == EXIT_CONFIRMED
        elapsed = time.monotonic() - started
    finally:
        shell.stop()

    assert elapsed < 10.0
    body = json.loads(capsys.readouterr().out)
    assert body.get("client_open") is True


@pytest.mark.requirement("AGENT-12")
def test_no_performance_page_names_remedy_when_shell_never_opens(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] the shell never opens a performance page [then] state names the open_route remedy, [else stop]."""
    started = time.monotonic()
    assert main(_argv(engine, "state")) == EXIT_NO_PAGE
    elapsed = time.monotonic() - started

    captured = capsys.readouterr()
    assert REMEDY_VERB in captured.err
    assert "open_route" in captured.err
    assert elapsed >= 9.0
