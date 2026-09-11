"""playwright_presence_check.py: nightly webkit suite must execute every listed test.

Regression lines:
  - if serial skip leaves 11 tests unrun then the checker fails with did not run
  - if the same defects all execute (failed, not skipped) then the checker passes
  - if the report is missing or empty then the checker never exits 0
  - if listed count is below --floor then the checker names both numbers
  - if a test is interrupted then it counts as did not run
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "playwright_presence_check.py"


def _run(report_path: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *extra, str(report_path)],
        capture_output=True,
        text=True,
        check=False,
    )


def _write_report(tmp_path: Path, payload: dict, name: str = "results.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _test_entry(
    *,
    title: str,
    status: str,
    project: str = "webkit",
    file_name: str = "webkit-deckload.spec.ts",
) -> dict:
    return {
        "projectName": project,
        "title": title,
        "results": [{"status": status}],
    }


def _suite_tree(tests: list[dict], *, nested: bool = False) -> dict:
    inner = {
        "title": "webkit-deckload.spec.ts",
        "file": "webkit-deckload.spec.ts",
        "specs": [{"title": "webkit performance controls", "tests": tests}],
    }
    if nested:
        return {"suites": [{"title": "root", "suites": [inner]}]}
    return {"suites": [inner]}


def test_serial_skip_shape_fails_with_did_not_run(tmp_path: Path) -> None:
    """12 passed + 1 failed + 11 skipped (#712 evidence) must not pass presence."""
    tests: list[dict] = []
    for i in range(12):
        tests.append(_test_entry(title=f"passing {i}", status="passed"))
    tests.append(_test_entry(title="the wave row exposes the decoded duration", status="failed"))
    for i in range(11):
        tests.append(_test_entry(title=f"skipped remainder {i}", status="skipped"))
    report = _write_report(tmp_path, _suite_tree(tests, nested=True))
    result = _run(report)
    assert result.returncode != 0
    combined = f"{result.stdout}\n{result.stderr}"
    assert "did not run" in combined
    assert "skipped remainder" in combined


def test_all_failed_still_executed_passes(tmp_path: Path) -> None:
    """Same defects but every test executed (failed, not skipped) must pass."""
    tests = [
        _test_entry(title=f"observed failure {i}", status="failed") for i in range(24)
    ]
    report = _write_report(tmp_path, _suite_tree(tests))
    result = _run(report)
    assert result.returncode == 0, result.stderr
    assert "0 did not run" in result.stdout


def test_all_passed_passes(tmp_path: Path) -> None:
    """Every listed test passed must pass presence."""
    tests = [_test_entry(title=f"ok {i}", status="passed") for i in range(5)]
    report = _write_report(tmp_path, _suite_tree(tests))
    result = _run(report)
    assert result.returncode == 0, result.stderr


def test_floor_above_listed_fails(tmp_path: Path) -> None:
    """--floor 41 with only 24 listed tests must name the floor."""
    tests = [_test_entry(title=f"t {i}", status="passed") for i in range(24)]
    report = _write_report(tmp_path, _suite_tree(tests))
    result = _run(report, "--floor", "41")
    assert result.returncode != 0
    assert "floor is 41" in result.stderr


def test_floor_below_listed_passes(tmp_path: Path) -> None:
    """--floor 13 with 13 listed (12 passed + 1 failed) must pass."""
    tests = [_test_entry(title=f"p {i}", status="passed") for i in range(12)]
    tests.append(_test_entry(title="failed one", status="failed"))
    report = _write_report(tmp_path, _suite_tree(tests))
    result = _run(report, "--floor", "13")
    assert result.returncode == 0, result.stderr


def test_missing_file_is_nonzero(tmp_path: Path) -> None:
    """Missing report must never exit 0."""
    missing = tmp_path / "missing.json"
    result = _run(missing)
    assert result.returncode == 2
    assert "missing" in result.stderr.lower()


def test_empty_object_report_is_nonzero(tmp_path: Path) -> None:
    """{} or no suites must not pass."""
    report = _write_report(tmp_path, {})
    result = _run(report)
    assert result.returncode != 0
    assert "0 tests" in result.stderr


def test_interrupted_counts_as_did_not_run(tmp_path: Path) -> None:
    """interrupted must fail presence like skipped."""
    tests = [
        _test_entry(title="started", status="passed"),
        _test_entry(title="aborted", status="interrupted"),
    ]
    report = _write_report(tmp_path, _suite_tree(tests))
    result = _run(report)
    assert result.returncode != 0
    assert "did not run" in f"{result.stdout}\n{result.stderr}"
    assert "interrupted" in result.stderr
