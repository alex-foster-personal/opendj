"""secscan semgrep-summary: expected diff scope is UNKNOWN when semgrep under-reports.

- if a scannable diff loads 0 rules with --expected-scannable > 0 then exit 2
- if baseline scan loads rules, scans 0 files, and has no errors or results then exit 0
- if files were scanned with rules loaded and no results then exit 0 with count 0
- if full mode sees 0 rules and 0 scanned without --expected-scannable then exit 2
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SECSCAN = Path(__file__).resolve().parents[2] / "scripts" / "security" / "secscan.py"


def _summary(tmp_path: Path, doc: dict, *flags: str) -> tuple[int, str]:
    scan = tmp_path / "scan.json"
    scan.write_text(json.dumps(doc), encoding="utf-8")
    count = tmp_path / "scan.count"
    proc = subprocess.run(
        [sys.executable, str(SECSCAN), "semgrep-summary", str(scan), "--min-rules", "1",
         "--min-files", "1", "--count-file", str(count), *flags],
        capture_output=True, text=True, check=False,
    )
    return proc.returncode, count.read_text(encoding="utf-8").strip() if count.exists() else ""


def _doc(rules: int, scanned: int) -> dict:
    return {
        "results": [],
        "errors": [],
        "time": {"rules": [{"id": f"r{i}"} for i in range(rules)]},
        "paths": {"scanned": [f"f{i}.py" for i in range(scanned)]},
    }


def test_baseline_clean_zero_scanned_passes(tmp_path: Path) -> None:
    """[if] baseline scan loads rules but scans 0 paths with no findings [then] pass, [else stop]."""
    rc, count = _summary(tmp_path, _doc(rules=154, scanned=0), "--expected-scannable", "3")
    assert rc == 0
    assert count == "0"


def test_zero_scanned_with_semgrep_errors_is_unknown(tmp_path: Path) -> None:
    """[if] expected scope but semgrep errors with 0 scanned [then] UNKNOWN, [else stop]."""
    doc = _doc(rules=5, scanned=0)
    doc["errors"] = [{"level": "error", "type": "SemgrepError", "message": "boom"}]
    rc, _ = _summary(tmp_path, doc, "--expected-scannable", "3")
    assert rc == 2


def test_python_diff_empty_rules_is_unknown(tmp_path: Path) -> None:
    rc, _ = _summary(tmp_path, _doc(rules=0, scanned=0), "--expected-scannable", "1")
    assert rc == 2, "a scannable diff with an empty rule set must read UNKNOWN"


def test_empty_scan_without_expected_scannable_is_unknown(tmp_path: Path) -> None:
    rc, _ = _summary(tmp_path, _doc(rules=0, scanned=0))
    assert rc == 2, "outside diff-aware mode an empty scan is unmeasured, never a pass"


def test_files_scanned_with_no_rules_is_unknown_with_expected_scannable(tmp_path: Path) -> None:
    rc, _ = _summary(tmp_path, _doc(rules=0, scanned=3), "--expected-scannable", "3")
    assert rc == 2, "rules that failed to load must not hide behind a scannable diff"


def test_clean_scan_passes(tmp_path: Path) -> None:
    rc, count = _summary(tmp_path, _doc(rules=154, scanned=3), "--expected-scannable", "3")
    assert rc == 0
    assert count == "0"
