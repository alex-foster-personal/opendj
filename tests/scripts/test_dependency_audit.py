"""Tests for scripts/dependency_audit (row 8, #1507).

Acceptance:
- [if] a known-CVE dependency is in the closure [then] the audit names advisory id and path
- [if] the audit tool itself fails to run [then] UNKNOWN and non-zero exit
- [if] a finding is dev-only [then] the report says so
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from scripts import dependency_audit as mod
from scripts.dependency_audit_core import (
    EXIT_FINDINGS,
    EXIT_OK,
    EXIT_UNKNOWN,
    Finding,
    ScanResult,
    parse_cargo_audit_json,
    parse_pip_audit_json,
    parse_pnpm_audit_json,
    summarize,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "periodic-checks.yml"
MODULE = "scripts.dependency_audit"
ROW = "8"


def test_pip_audit_parser_names_cve_and_path() -> None:
    payload = json.dumps(
        {
            "dependencies": [
                {
                    "name": "pillow",
                    "version": "10.0.0",
                    "vulns": [{"id": "PYSEC-2024-1", "fix_versions": ["10.4.0"]}],
                }
            ]
        }
    )
    result = parse_pip_audit_json(payload, ecosystem="python-test")
    assert result.status == "FINDINGS"
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.advisory_id == "PYSEC-2024-1"
    assert "pillow" in finding.path
    assert finding.dev_only is False


def test_pip_audit_empty_dependencies_is_ok_not_unknown() -> None:
    result = parse_pip_audit_json('{"dependencies": []}', ecosystem="python-test")
    assert result.status == "OK"


def test_pip_audit_invalid_json_is_unknown() -> None:
    result = parse_pip_audit_json("not-json", ecosystem="python-test")
    assert result.status == "UNKNOWN"


def test_pnpm_audit_labels_dev_only_paths() -> None:
    payload = json.dumps(
        {
            "advisories": {
                "109": {
                    "cves": ["CVE-2026-0001"],
                    "module_name": "vite",
                    "findings": [
                        {
                            "version": "5.4.0",
                            "paths": [".>@storybook/sveltekit>vite", ".>@sveltejs/kit>vite"],
                        }
                    ],
                }
            }
        }
    )
    result = parse_pnpm_audit_json(payload, ecosystem="js-test")
    assert result.status == "FINDINGS"
    scopes = {f.dev_only for f in result.findings}
    assert scopes == {True, False}
    assert all(f.advisory_id for f in result.findings)
    assert all(f.path for f in result.findings)


def test_cargo_audit_parser_names_rustsec_and_path() -> None:
    payload = json.dumps(
        {
            "vulnerabilities": {
                "list": [
                    {
                        "advisory": {"id": "RUSTSEC-2024-0001"},
                        "package": {"name": "openssl", "version": "0.10.0"},
                        "dependencies": ["crate-a", "crate-b"],
                    }
                ]
            }
        }
    )
    result = parse_cargo_audit_json(payload, ecosystem="rust-test")
    assert result.status == "FINDINGS"
    assert result.findings[0].advisory_id == "RUSTSEC-2024-0001"
    assert "crate-a" in result.findings[0].path


def test_summarize_unknown_beats_findings() -> None:
    code, text = summarize(
        [
            ScanResult("a", "FINDINGS", (Finding("x", "CVE-1", "p", "path", False),)),
            ScanResult("b", "UNKNOWN", notes=("tool failed",)),
        ]
    )
    assert code == EXIT_UNKNOWN
    assert "UNKNOWN scans: b" in text


def test_run_all_unknown_when_tool_produces_no_output() -> None:
    def _runner() -> list[ScanResult]:
        return [ScanResult("broken", "UNKNOWN", notes=("pip-audit produced no output",))]

    code, _, text = mod.run_all(runner=_runner)
    assert code == EXIT_UNKNOWN
    assert "UNKNOWN" in text


def test_run_all_findings_exit_one() -> None:
    def _runner() -> list[ScanResult]:
        return [
            ScanResult("ok", "OK"),
            ScanResult(
                "js",
                "FINDINGS",
                (
                    Finding(
                        ecosystem="js",
                        advisory_id="GHSA-xxxx",
                        package="vite@5.0",
                        path=".>vite",
                        dev_only=True,
                    ),
                ),
            ),
        ]

    code, _, text = mod.run_all(runner=_runner)
    assert code == EXIT_FINDINGS
    assert "dev-only=1" in text
    assert "[dev-only]" in text


def test_run_all_clean_exit_zero() -> None:
    code, _, _ = mod.run_all(runner=lambda: [ScanResult("ok", "OK")])
    assert code == EXIT_OK


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_periodic_workflow_has_monthly_dependency_audit_job() -> None:
    jobs = _workflow()["jobs"]
    assert "dependency-audit" in jobs, "row 8 job missing from periodic-checks.yml"
    job = jobs["dependency-audit"]
    condition = str(job.get("if", ""))
    assert "workflow_dispatch" in condition or "schedule" in condition
    assert "dependency_audit" in json.dumps(job)


def test_dependency_audit_not_gated_on_merge_window() -> None:
    job = _workflow()["jobs"]["dependency-audit"]
    condition = str(job.get("if", ""))
    assert "needs.window.outputs.run" not in condition
    needs = job.get("needs") or []
    assert "window" not in needs


def test_ledger_report_job_for_row_8() -> None:
    jobs = _workflow()["jobs"]
    assert "dependency-audit-report" in jobs
    report = jobs["dependency-audit-report"]
    script = "\n".join(str(step.get("run", "")) for step in report.get("steps") or [])
    assert f"row {ROW}" in script.lower() or f"row={ROW}" in script
    assert "1492" in script
