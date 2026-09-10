"""The CI wiring the reqs flip gate needs to be a gate at all (issue #1605).

`scripts/reqs_flip_gate.py` has its own unit tests. This module tests the four
properties of its WORKFLOW that decide whether the script's verdict is worth
anything, all four of which were defects on PR #1709's first two heads and none
of which any script-level test can see.

Requirements:
- ✔︎ ✅ 🎯 The gate runs on every pull request, docs-only ones included.
- ✔︎ ✅ 🎯 The gate re-runs when PR metadata changes, not only when the head moves.
- ✔︎ ✅ 🎯 The job running the gate can actually read the pull request.
- ✔︎ ✅ 🎯 The gate does not also run from a path-filtered workflow.

Acceptance tests:
- [if] the gate's workflow grows a `paths` or `paths-ignore` filter [then] this
  fails [⛔️ if a docs-only PR can cite a pending id with no check scheduled at
  all, which is what the gate exists to catch and reads as no objection].
- [if] its `pull_request` types omit `edited` [then] this fails [⛔️ if an author
  can pass the gate and then edit the reason out of the body with no new head
  SHA, leaving the green check standing at merge time].
- [if] the types list drops one of GitHub's three defaults [then] this fails
  [⛔️ if naming `types` at all silently narrows when the gate runs].
- [if] the job stops declaring `pull-requests: read` [then] this fails [⛔️ if
  `gh pr view` goes back to "Resource not accessible by integration" and the
  gate can only report UNKNOWN].
- [if] it declares permissions without `contents: read` [then] this fails
  [⛔️ if naming a block replaces the default and breaks actions/checkout].
- [if] `ci.yml` runs the gate again [then] this fails [⛔️ if the path-filtered
  copy comes back beside the unfiltered one and a docs PR's absent check is
  read as this gate having run].

-Claude
"""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
GATE = WORKFLOWS / "reqs-flip-gate.yml"
CI = WORKFLOWS / "ci.yml"

# GitHub's implicit `pull_request` type list. Naming `types` replaces it, so a
# regression here is a NARROWING, not just a missing entry.
GITHUB_DEFAULT_TYPES = {"opened", "synchronize", "reopened"}

GATE_MODULE = "scripts.reqs_flip_gate"


def _doc(path: Path) -> dict:
    # PyYAML reads the bare `on:` key as the boolean True, not the string "on",
    # so callers ask for both rather than relying on one of them existing.
    return yaml.safe_load(path.read_text())


def _triggers(doc: dict) -> dict:
    return doc.get("on", doc.get(True))


def _runs_the_gate(doc: dict) -> bool:
    return any(
        GATE_MODULE in str(step.get("run", ""))
        for job in (doc.get("jobs") or {}).values()
        for step in (job.get("steps") or [])
    )


def test_the_gate_workflow_actually_runs_the_gate() -> None:
    """Guards every other test here: a subject that runs nothing proves nothing."""
    assert GATE.exists(), f"{GATE.name} is missing; the gate has no workflow"
    assert _runs_the_gate(_doc(GATE)), (
        f"no step in {GATE.name} runs {GATE_MODULE}; if the gate moved, move "
        "this contract with it rather than deleting it"
    )


def test_the_gate_runs_on_docs_only_pull_requests() -> None:
    """The one PR shape the gate exists for must not be filtered out of it."""
    pull_request = _triggers(_doc(GATE))["pull_request"]

    for key in ("paths", "paths-ignore"):
        assert key not in pull_request, (
            f"{GATE.name} declares `{key}`, so some PRs schedule no run at all "
            "and produce no check. A docs-only PR citing a pending v1 id it "
            "does not flip is exactly what this gate is for, and an absent "
            "check reads as no objection everywhere it is looked at."
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
        f"naming `types` replaces GitHub's defaults, and {sorted(missing)} "
        "would no longer trigger the gate at all"
    )


def test_the_job_running_the_gate_can_read_the_pull_request() -> None:
    doc = _doc(GATE)
    # Either scope works on GitHub; read whichever the file declares, and refuse
    # to pass on a job that inherits nothing.
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
    """ci.yml is path-filtered, which is why the gate left it."""
    assert not _runs_the_gate(_doc(CI)), (
        "ci.yml runs the reqs flip gate again. It is path-filtered, so its "
        "copy is absent on exactly the docs-only PRs the gate exists for, and "
        "an absent check is indistinguishable from a passing one"
    )
