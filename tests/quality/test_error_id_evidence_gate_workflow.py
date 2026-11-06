"""The CI wiring the error-id evidence gate needs to be a gate at all (issue #2325).

`scripts/error_id_evidence_check.py` has its own unit tests. This module tests the
same four properties as `test_reqs_flip_gate_workflow.py`, plus the job id and
draft-skip `if:` match to `ci_tests`.

-Claude
"""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
GATE = WORKFLOWS / "pr-body-gates.yml"
CI = WORKFLOWS / "ci.yml"

GITHUB_DEFAULT_TYPES = {"opened", "synchronize", "reopened"}

GATE_MODULE = "scripts.error_id_evidence_check"


def _doc(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _triggers(doc: dict) -> dict:
    triggers = doc.get("on", doc.get(True))
    if not isinstance(triggers, dict):
        raise TypeError("workflow on: is not a mapping")
    return triggers


def _runs_the_gate(doc: dict) -> bool:
    return any(
        GATE_MODULE in str(step.get("run", ""))
        for job in (doc.get("jobs") or {}).values()
        for step in (job.get("steps") or [])
    )


def test_the_gate_workflow_actually_runs_the_gate() -> None:
    assert GATE.exists(), f"{GATE.name} is missing; the gate has no workflow"
    jobs = _doc(GATE)["jobs"]
    assert "error_id_evidence" in jobs, f"{GATE.name} is missing job `error_id_evidence`"
    assert _runs_the_gate(_doc(GATE)), (
        f"no step in {GATE.name} runs {GATE_MODULE}; if the gate moved, move "
        "this contract with it rather than deleting it"
    )


def test_the_gate_runs_on_docs_only_pull_requests() -> None:
    pull_request = _triggers(_doc(GATE))["pull_request"]

    for key in ("paths", "paths-ignore"):
        assert key not in pull_request, (
            f"{GATE.name} declares `{key}`, so some PRs schedule no run at all "
            "and produce no check."
        )


def test_the_gate_reruns_when_pr_metadata_is_edited() -> None:
    types = set(_triggers(_doc(GATE))["pull_request"]["types"])

    assert "edited" in types, (
        "the gate reads the PR title and body, which change with no new head "
        "SHA, so without `edited` a passing check outlives the metadata that "
        "earned it"
    )
    missing = GITHUB_DEFAULT_TYPES - types
    assert not missing, (
        f"naming `types` replaces GitHub's defaults, and {sorted(missing)} would no longer trigger the gate"
    )


def test_the_job_running_the_gate_can_read_the_pull_request() -> None:
    doc = _doc(GATE)
    job = next(j for j in doc["jobs"].values() if _runs_the_gate({"jobs": {"gate": j}}))
    permissions = job.get("permissions") or doc.get("permissions")

    assert permissions is not None, (
        "the gate needs an explicit permissions block: this repository's "
        "default workflow permissions are `read`, which covers contents and "
        "packages only, so `gh pr view` cannot reach the API"
    )
    assert permissions.get("pull-requests") == "read", (
        "the gate must declare `pull-requests: read` or it can only ever "
        "report UNKNOWN (exit 2), which reddens the check on every PR"
    )
    assert permissions.get("contents") == "read", (
        "naming a permissions block REPLACES the default, so `contents: read` "
        "has to be restated or actions/checkout loses access"
    )


def test_ci_does_not_run_the_gate_a_second_time() -> None:
    assert not _runs_the_gate(_doc(CI)), (
        "ci.yml runs the error-id evidence gate again. It is path-filtered, so its "
        "copy is absent on exactly the docs-only PRs the gate exists for"
    )


def test_error_id_evidence_job_matches_ci_tests_skip() -> None:
    jobs = _doc(GATE)["jobs"]
    assert jobs["error_id_evidence"]["if"] == jobs["ci_tests"]["if"]
