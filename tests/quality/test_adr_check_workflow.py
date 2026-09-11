"""The CI wiring the ADR gate needs to be a gate at all (issue #1489).

`scripts/adr_check.py` has its own unit tests. This module tests the
workflow properties that decide whether the script's verdict is worth
anything, copied from the reqs-flip-gate contract (PR #1709):

- the gate runs on every pull request, docs-only ones included
- the gate re-runs when PR metadata changes, not only when the head moves
- the job can actually read the pull request
- the gate does not also run from a path-filtered workflow

-Claude
"""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
GATE = WORKFLOWS / "adr-check.yml"
CI = WORKFLOWS / "ci.yml"

GITHUB_DEFAULT_TYPES = {"opened", "synchronize", "reopened"}
GATE_MODULE = "scripts.adr_check"


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
    assert _runs_the_gate(_doc(GATE)), (
        f"no step in {GATE.name} runs {GATE_MODULE}; if the gate moved, move "
        "this contract with it rather than deleting it"
    )


def test_the_gate_has_no_path_filter() -> None:
    pr = _triggers(_doc(GATE)).get("pull_request")
    assert isinstance(pr, dict), "pull_request trigger is not a mapping"
    assert "paths" not in pr and "paths-ignore" not in pr, (
        "a paths filter would skip docs-only PRs, which still need the ADR line "
        "to be checkable when they later grow a gated file"
    )


def test_the_gate_reruns_on_body_edits() -> None:
    pr = _triggers(_doc(GATE)).get("pull_request")
    types = set(pr.get("types") or [])
    assert types >= GITHUB_DEFAULT_TYPES, (
        f"naming types replaced GitHub's defaults; missing {GITHUB_DEFAULT_TYPES - types}"
    )
    assert "edited" in types, (
        "without edited, a green check outlives a body that had its ADR line removed"
    )


def test_the_job_can_read_pull_requests() -> None:
    perms = _doc(GATE).get("permissions") or {}
    assert perms.get("pull-requests") == "read"
    assert perms.get("contents") == "read"


def test_ci_yml_does_not_also_run_the_gate() -> None:
    assert not _runs_the_gate(_doc(CI)), (
        "ci.yml is path-filtered; a second copy there would make a docs PR's "
        "absent check look like this gate had run"
    )
