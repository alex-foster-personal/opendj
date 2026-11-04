"""``opendj dj show`` on a plan nested too deeply for the JSON parser (AGENT-5858)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.opendj_cli import EXIT_UNCONFIRMED
from apps.opendj_cli.dj_cli import run


@pytest.mark.requirement("AGENT-26")
def test_show_refuses_a_deeply_nested_plan_as_plan_invalid(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """[if] show gets a plan nested past the parser's stack [then] it answers plan_invalid, [else stop]."""
    plan = tmp_path / "deep.json"
    plan.write_text('{"plan": ' + "[" * 5000 + "]" * 5000 + "}", encoding="utf-8")
    argv = ["show", str(plan), "--as", "json"]
    code = run(argv, lock=None)
    assert code == EXIT_UNCONFIRMED
    assert json.loads(capsys.readouterr().out)["error"] == "plan_invalid"
