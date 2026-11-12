"""WINSTATE-01: ``opendj window state|reset`` reads and clears the shell's saved geometry.

[if] `opendj window state|reset` does not read or clear the shell's saved geometry [then] a bad window position cannot be inspected or recovered without the UI, [else stop].

SECONDSCREEN-11715: [if] `opendj window state` does not list the second-screen window's own geometry
[then] the library window's saved placement cannot be inspected, [else stop].
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED
from apps.opendj_cli.__main__ import main

pytestmark = [pytest.mark.requirement("WINSTATE-01"), pytest.mark.requirement("SECONDSCREEN-11715")]

SAVED = {
    "version": 1,
    "last_monitor": "LG|3840x2160|@1.50",
    "monitors": {
        "LG|3840x2160|@1.50": {
            "x_offset_pt": 100.0,
            "y_offset_pt": 50.0,
            "width_pt": 1800.0,
            "height_pt": 1100.0,
            "maximized": False,
        }
    },
}


def _lock(tmp_path: Path) -> Path:
    return tmp_path / ".engine.lock"


def test_state_reads_the_file_beside_the_lock(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "window-state.json").write_text(json.dumps(SAVED), encoding="utf-8")
    code = main(["--json", "--lock", str(_lock(tmp_path)), "window", "state"])
    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_CONFIRMED, "if opendj window state fails on a saved file then broken"
    assert payload["saved"] is True
    assert payload["windows"]["main"]["monitors"] == SAVED["monitors"], (
        "if per-monitor geometry is not reported then broken"
    )
    assert payload["windows"]["second-screen"] == {"last_monitor": None, "monitors": {}}


def test_state_lists_each_window_label_with_its_own_geometry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    desk = {"x_offset_pt": 20.0, "y_offset_pt": 30.0, "width_pt": 1400.0, "height_pt": 900.0, "maximized": False}
    document = {
        "version": 2,
        "windows": {
            "main": {"last_monitor": SAVED["last_monitor"], "monitors": SAVED["monitors"]},
            "second-screen": {"last_monitor": "Desk|2560x1440|@1.00", "monitors": {"Desk|2560x1440|@1.00": desk}},
        },
    }
    (tmp_path / "window-state.json").write_text(json.dumps(document), encoding="utf-8")
    assert main(["--json", "--lock", str(_lock(tmp_path)), "window", "state"]) == EXIT_CONFIRMED
    payload = json.loads(capsys.readouterr().out)
    assert payload["windows"]["main"]["monitors"] == SAVED["monitors"]
    assert payload["windows"]["second-screen"]["monitors"] == {"Desk|2560x1440|@1.00": desk}, (
        "if the second-screen geometry is not listed then broken"
    )
    assert main(["--lock", str(_lock(tmp_path)), "window", "state"]) == EXIT_CONFIRMED
    text = capsys.readouterr().out
    assert "main:" in text and "second-screen:" in text and "1400x900" in text


def test_state_before_first_save_is_empty_not_an_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["--json", "--lock", str(_lock(tmp_path)), "window", "state"])
    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_CONFIRMED
    assert payload["saved"] is False
    assert all(payload["windows"][label]["monitors"] == {} for label in ("main", "second-screen"))


def test_reset_removes_the_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    state_file = tmp_path / "window-state.json"
    state_file.write_text(json.dumps(SAVED), encoding="utf-8")
    code = main(["--json", "--lock", str(_lock(tmp_path)), "window", "reset"])
    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_CONFIRMED and payload["existed"] is True
    assert not state_file.exists(), "if opendj window reset leaves the file then broken"


def test_unknown_verb_and_corrupt_file_are_named_refusals(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--json", "--lock", str(_lock(tmp_path)), "window", "resize"]) == EXIT_FAILED
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "usage"
    (tmp_path / "window-state.json").write_text("{not json", encoding="utf-8")
    assert main(["--json", "--lock", str(_lock(tmp_path)), "window", "state"]) == EXIT_FAILED
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "window_state_unreadable"
