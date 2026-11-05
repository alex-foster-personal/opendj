"""Mergify is fully removed; Trunk is the only merge queue and test-health sink.

Regression lines:
  - if `.mergify.yml` returns then broken
  - if a workflow step uses a Mergify action or `MERGIFY_TOKEN` then a credential for an
    uninstalled app is wired into CI again
  - if `ADR-NEW-mergify-queue-sole-merge-authority.md` is `accepted` then the dead
    Mergify-as-authority decision reads as live
  - if the landing ADR is not `accepted` then this removal has no in-force record
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from scripts.adr_allocate import _parse_status

REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO / ".github" / "workflows"
DECISIONS = REPO / "docs" / "decisions"

SUPERSEDED_MERGIFY_ADRS = (
    "ADR-0078-mergify-ci-insights-test-health.md",
    "ADR-NEW-ci-insights-cost-gate.md",
    "ADR-NEW-mergify-queue-staged-optin.md",
    "ADR-NEW-mergify-queue-sole-merge-authority.md",
)
LANDING_ADR = "ADR-NEW-trunk-is-the-only-merge-queue.md"
IMPLEMENTED_QUEUE_ADR = "ADR-NEW-trunk-merge-queue-replaces-mergify.md"

_MERGIFY_STEP_MARKERS = (
    "MERGIFY_TOKEN",
    "mergify-token",
    "api.mergify.com",
)


def test_the_root_mergify_config_file_is_gone() -> None:
    assert not (REPO / ".mergify.yml").exists()
    tracked = subprocess.run(
        ["git", "ls-files", "--", ".mergify.yml"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert tracked == "", "`.mergify.yml` must not be tracked"
    ci_tracked = subprocess.run(
        ["git", "ls-files", "--", ".github/workflows/ci.yml"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert ci_tracked, "positive control: ci.yml is tracked"


def test_no_workflow_step_talks_to_mergify() -> None:
    workflow_files = sorted(WORKFLOWS.glob("*.yml"))
    assert workflow_files, "positive control: at least one workflow file exists"
    ci_text = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "trunk-io/analytics-uploader@" in ci_text, "positive control: Trunk uploader in ci.yml"

    parsed_any = False
    for path in workflow_files:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            continue
        parsed_any = True
        jobs = document.get("jobs") or {}
        for job_id, job in jobs.items():
            if not isinstance(job, dict):
                continue
            steps = job.get("steps") or []
            for step in steps:
                if not isinstance(step, dict):
                    continue
                uses = str(step.get("uses", ""))
                assert "mergify" not in uses.lower(), f"{path.name} job {job_id} uses a Mergify action: {uses!r}"
                step_blob = str(step)
                for marker in _MERGIFY_STEP_MARKERS:
                    assert marker not in step_blob, f"{path.name} job {job_id} step references {marker!r}"
    assert parsed_any, "positive control: at least one workflow parsed as a mapping"


def test_dead_mergify_adrs_are_superseded_and_trunk_adrs_accepted() -> None:
    for name in SUPERSEDED_MERGIFY_ADRS:
        path = DECISIONS / name
        assert path.is_file(), f"positive control: {name} exists"
        status = _parse_status(path.read_text(encoding="utf-8"))
        assert status.startswith("superseded-by"), f"{name} must be superseded, got {status!r}"

    for name in (LANDING_ADR, IMPLEMENTED_QUEUE_ADR):
        path = DECISIONS / name
        assert path.is_file(), f"positive control: {name} exists"
        status = _parse_status(path.read_text(encoding="utf-8"))
        assert status == "accepted", f"{name} must be accepted, got {status!r}"
