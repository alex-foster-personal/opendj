"""``opendj hotkeys`` lists the overlay rows from the exported registry contract (#12287).

[if] the CLI runs [then] every contract row prints with its id, chord and description, [else stop].
[if] the platform is non-Apple [then] modifier labels say Ctrl, never Cmd+, [else stop].
[if] a row's build flag is off [then] the row is marked unavailable, [else stop].
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from apps.feature_flags import FLAGS_FILE_ENV
from apps.opendj_cli import EXIT_CONFIRMED
from apps.opendj_cli.__main__ import main
from apps.opendj_cli.hotkeys import CONTRACT_PATH

_ASSISTANT_ROW = "assistant-toggle"
_ASSISTANT_FLAG = "assistant.sidebar"


def _contract_rows() -> list[dict]:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))["rows"]


def _run_json(argv: list[str], capsys: pytest.CaptureFixture[str]) -> dict:
    assert main(["--json", "hotkeys", *argv]) == EXIT_CONFIRMED
    return json.loads(capsys.readouterr().out)


@pytest.fixture
def assistant_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the flag store at a scratch file and return a setter for the assistant flag."""
    flags_file = tmp_path / "feature-flags.json"
    monkeypatch.setenv(FLAGS_FILE_ENV, str(flags_file))

    def _set(enabled: bool) -> None:
        flags_file.write_text(json.dumps({_ASSISTANT_FLAG: enabled}), encoding="utf-8")

    _set(True)
    return _set


# REQ: KEYB-14154
@pytest.mark.requirement("KEYB-14154")
def test_hotkeys_prints_every_contract_row(assistant_flag, capsys: pytest.CaptureFixture[str]) -> None:
    """[if] an operator runs opendj hotkeys [then] each contract row prints its id, chord and description, [else stop]."""
    contract = _contract_rows()
    assert main(["hotkeys", "--platform", "mac"]) == EXIT_CONFIRMED
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == len(contract)
    for line, row in zip(lines, contract, strict=True):
        assert line.split()[0] == row["id"]
        assert f"  {row['mac']['chord']}  " in line
        assert line.rstrip().endswith(row["mac"]["description"])
    assert {"settings-open", "playlist-undo", "selector.move.up"} <= {row["id"] for row in contract}


# REQ: KEYB-14154
@pytest.mark.requirement("KEYB-14154")
def test_hotkeys_non_apple_platform_uses_ctrl(assistant_flag, capsys: pytest.CaptureFixture[str]) -> None:
    """[if] --platform other is asked for [then] no chord or description names Cmd as a modifier, [else stop]."""
    rows = {row["id"]: row for row in _run_json(["--platform", "other"], capsys)["rows"]}
    assert rows["settings-open"]["chord"] == "Ctrl+,"
    assert rows["playlist-redo"]["chord"] == "Ctrl+Shift+Z"
    assert rows[_ASSISTANT_ROW]["chord"] == "Ctrl+I"
    primary_cmd = re.compile(r"\bCmd\+")
    assert not [row["id"] for row in rows.values() if primary_cmd.search(row["chord"] + row["description"])]
    mac_rows = {row["id"]: row for row in _run_json(["--platform", "mac"], capsys)["rows"]}
    assert mac_rows["settings-open"]["chord"] == "Cmd+,"


# REQ: KEYB-14154
@pytest.mark.requirement("KEYB-14154")
def test_hotkeys_marks_flag_gated_row_unavailable(assistant_flag, capsys: pytest.CaptureFixture[str]) -> None:
    """[if] the assistant.sidebar flag is off [then] its Cmd+I row prints as unavailable, not as working, [else stop]."""
    assistant_flag(False)
    rows = {row["id"]: row for row in _run_json(["--platform", "mac"], capsys)["rows"]}
    assert rows[_ASSISTANT_ROW]["feature_flag"] == _ASSISTANT_FLAG
    assert rows[_ASSISTANT_ROW]["available"] is False
    assert [row_id for row_id, row in rows.items() if not row["available"]] == [_ASSISTANT_ROW]
    assert main(["hotkeys", "--platform", "mac"]) == EXIT_CONFIRMED
    text = capsys.readouterr().out
    assert f"[unavailable: feature flag {_ASSISTANT_FLAG} is off]" in next(
        line for line in text.splitlines() if line.startswith(_ASSISTANT_ROW)
    )
    assistant_flag(True)
    rows = {row["id"]: row for row in _run_json(["--platform", "mac"], capsys)["rows"]}
    assert rows[_ASSISTANT_ROW]["available"] is True
