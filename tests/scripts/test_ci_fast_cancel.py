"""``scripts.ci_fast_cancel`` cancels a run only on a failure that is NEW against main.

Single-line intent:
  - if a failure absent from main's red set does not cancel the run then broken
  - if a failure that is also red on main cancels the run then broken
  - if a missing baseline cancels anything, or exits 0, then broken
  - if a log with no failure identity cancels the run then broken
  - if --dry-run calls gh then broken

[if] the fast tier fails on a genuine identity [then] the run is cancelled, [else stop].
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import ci_fast_cancel

pytestmark = pytest.mark.requirement("INFRA-03")

LOG = """\
tests/a/test_x.py::test_one PASSED
=========================== short test summary info ============================
FAILED tests/a/test_x.py::test_two - assert 0 == 1
ERROR tests/b/test_y.py::test_three[param-1] - fixture broke
= 2 failed, 1 passed in 3.21s =
"""


def _baseline(tmp_path: Path, identities: list[str]) -> Path:
    path = tmp_path / "main-red.json"
    shape = {
        "identities": identities,
        "failed_job_names": [],
        "main_sha": "abc",
        "unreadable_job_names": [],
    }
    path.write_text(json.dumps(shape), encoding="utf-8")
    return path


def _log(tmp_path: Path, text: str = LOG) -> Path:
    path = tmp_path / "pytest.log"
    path.write_text(text, encoding="utf-8")
    return path


def _forbid_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    def _refuse(*_a: object, **_k: object) -> None:
        pytest.fail("gh was called")

    monkeypatch.setattr(ci_fast_cancel.subprocess, "run", _refuse)


def test_failed_identities_read_pytest_summary_lines() -> None:
    """if the ERROR line or a parametrized id is dropped then a genuine red reads as none"""
    assert ci_fast_cancel.failed_identities(LOG) == {
        "tests/a/test_x.py::test_two",
        "tests/b/test_y.py::test_three[param-1]",
    }


def test_genuine_failure_cancels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """if a failure absent from main's red set does not cancel the run then broken"""
    calls: list[list[str]] = []
    monkeypatch.setattr(ci_fast_cancel.subprocess, "run", lambda cmd, **_: calls.append(cmd))
    log = _log(tmp_path)
    baseline = _baseline(tmp_path, ["tests/a/test_x.py::test_two"])
    rc = ci_fast_cancel.main(
        ["--log", str(log), "--run-id", "777", "--main-red-json", str(baseline)]
    )
    assert rc == 0
    assert calls == [["gh", "run", "cancel", "777"]]
    out = capsys.readouterr().out
    assert "genuine tests/b/test_y.py::test_three[param-1]" in out
    assert "cancelled run 777" in out


def test_all_known_on_main_does_not_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """if a failure that is also red on main cancels the run then broken"""
    _forbid_gh(monkeypatch)
    log = _log(tmp_path)
    baseline = _baseline(
        tmp_path, ["tests/a/test_x.py::test_two", "tests/b/test_y.py::test_three[param-1]"]
    )
    rc = ci_fast_cancel.main(
        ["--log", str(log), "--run-id", "777", "--main-red-json", str(baseline)]
    )
    assert rc == 0
    assert "cancelling nothing" in capsys.readouterr().out


def test_missing_baseline_is_unknown_and_cancels_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """if a missing baseline cancels anything, or exits 0, then broken"""
    _forbid_gh(monkeypatch)
    log = _log(tmp_path)
    absent = tmp_path / "absent.json"
    rc = ci_fast_cancel.main(
        ["--log", str(log), "--run-id", "777", "--main-red-json", str(absent)]
    )
    assert rc == ci_fast_cancel.EXIT_UNKNOWN
    assert "UNKNOWN" in capsys.readouterr().out


def test_no_identity_does_not_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """if a log with no failure identity (cap kill) cancels the run then broken"""
    _forbid_gh(monkeypatch)
    log = _log(tmp_path, "Terminated\n")
    baseline = _baseline(tmp_path, [])
    rc = ci_fast_cancel.main(
        ["--log", str(log), "--run-id", "777", "--main-red-json", str(baseline)]
    )
    assert rc == 0
    assert "no failing test identity" in capsys.readouterr().out


def test_dry_run_never_calls_gh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """if --dry-run calls gh then broken"""
    _forbid_gh(monkeypatch)
    log = _log(tmp_path)
    baseline = _baseline(tmp_path, [])
    rc = ci_fast_cancel.main(
        ["--log", str(log), "--run-id", "777", "--main-red-json", str(baseline), "--dry-run"]
    )
    assert rc == 0
    assert "dry run, gh not called" in capsys.readouterr().out
