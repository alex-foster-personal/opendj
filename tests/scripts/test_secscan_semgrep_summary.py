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
    rc, count, _ = _summary_with_stderr(tmp_path, doc, *flags)
    return rc, count


def _summary_with_stderr(tmp_path: Path, doc: dict, *flags: str) -> tuple[int, str, str]:
    scan = tmp_path / "scan.json"
    scan.write_text(json.dumps(doc), encoding="utf-8")
    count = tmp_path / "scan.count"
    proc = subprocess.run(
        [sys.executable, str(SECSCAN), "semgrep-summary", str(scan), "--min-rules", "1",
         "--min-files", "1", "--count-file", str(count), *flags],
        capture_output=True, text=True, check=False,
    )
    written = count.read_text(encoding="utf-8").strip() if count.exists() else ""
    return proc.returncode, written, proc.stderr


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


# ----- the shape job 104882711482 produced on PR #3362 -------------------------------------------
# semgrep reports per-rule timings only for files it scanned, so an all-ignored diff arrives
# here as 0 rules AND 0 scanned. The fix removes that arrival (semgrep-diff-scope now counts 0
# and scan_sast.sh writes SKIP), and these two lock the summary control in place meanwhile:
# nothing about an unmeasured scan may read as a pass, with or without skipped evidence.


def _ignored_out_doc(skipped: list[dict] | None) -> dict:
    doc = _doc(rules=0, scanned=0)
    if skipped is not None:
        doc["paths"]["skipped"] = skipped
    return doc


def test_ignored_out_diff_is_unknown_even_with_skipped_evidence(tmp_path: Path) -> None:
    """[if] 0 rules, 0 scanned, and a skipped tests/ path [then] UNKNOWN, [else stop]."""
    doc = _ignored_out_doc(
        [{"path": "tests/scripts/test_probe.py", "reason": "semgrepignore_patterns_match"}]
    )
    rc, _, stderr = _summary_with_stderr(tmp_path, doc, "--expected-scannable", "1")
    assert rc == 2, "an unmeasured scan is UNKNOWN; the SKIP belongs upstream in diff scope"
    assert "ignore list" in stderr, "the reason must name the ignore list, not a rule-load failure"


def test_zero_scanned_without_skipped_evidence_is_unknown(tmp_path: Path) -> None:
    """[if] 0 rules and 0 scanned with no skipped evidence [then] UNKNOWN, [else stop]."""
    rc, _, _ = _summary_with_stderr(tmp_path, _ignored_out_doc(None), "--expected-scannable", "1")
    assert rc == 2, "absent evidence is not evidence of an ignored-out diff"


def test_rule_load_failure_still_names_the_rule_set(tmp_path: Path) -> None:
    """[if] files were scanned but 0 rules loaded [then] UNKNOWN blaming the rules, [else stop]."""
    rc, _, stderr = _summary_with_stderr(
        tmp_path, _doc(rules=0, scanned=3), "--expected-scannable", "3"
    )
    assert rc == 2
    assert "rule set failed to load" in stderr
