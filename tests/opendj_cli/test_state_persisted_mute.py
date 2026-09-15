"""Issue #2918: opendj state reports persisted master_muted from ui-prefs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.opendj_cli.__main__ import EXIT_CONFIRMED, main
from tests.opendj_cli.conftest import Engine
from tests.opendj_cli.test_cli_end_to_end import _argv


@pytest.mark.requirement("UXR-01")
def test_state_json_includes_persisted_master_muted(
    engine: Engine, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] CLI master_mute true [then] state --json reports persisted.master_muted true, [else stop]."""
    engine.app.state.data_dir = tmp_path / "data"
    page = engine.page()
    page.start()
    try:
        assert main(_argv(engine, "master_mute", "true")) == EXIT_CONFIRMED
        capsys.readouterr()
        assert main(_argv(engine, "--json", "state")) == EXIT_CONFIRMED
    finally:
        page.stop()

    body = json.loads(capsys.readouterr().out)
    assert body["persisted"]["master_muted"] is True
    assert body["master"]["muted"] is True
