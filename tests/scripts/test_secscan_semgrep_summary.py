"""secscan semgrep-summary: an empty diff is SKIP, a missing rule set is still UNKNOWN.

- if a diff-aware run scanned 0 files and loaded 0 rules under --skip-if-nothing-scanned then exit 3
- if the same empty run lacks that flag (full mode) then exit 2, never a pass
- if files were scanned but 0 rules loaded then exit 2 even with the flag
- if files were scanned with rules loaded and no results then exit 0 with count 0
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


def test_empty_diff_is_skip_with_the_flag(tmp_path: Path) -> None:
    rc, count = _summary(tmp_path, _doc(rules=0, scanned=0), "--skip-if-nothing-scanned")
    assert rc == 3, "an empty diff-aware scope must read SKIP"
    assert count == "0"


def test_empty_scan_without_the_flag_is_unknown(tmp_path: Path) -> None:
    rc, _ = _summary(tmp_path, _doc(rules=0, scanned=0))
    assert rc == 2, "outside diff-aware mode an empty scan is unmeasured, never a pass"


def test_files_scanned_with_no_rules_is_unknown_even_with_the_flag(tmp_path: Path) -> None:
    rc, _ = _summary(tmp_path, _doc(rules=0, scanned=3), "--skip-if-nothing-scanned")
    assert rc == 2, "rules that failed to load must not hide behind the empty-diff SKIP"


def test_clean_scan_passes(tmp_path: Path) -> None:
    rc, count = _summary(tmp_path, _doc(rules=154, scanned=3), "--skip-if-nothing-scanned")
    assert rc == 0
    assert count == "0"
