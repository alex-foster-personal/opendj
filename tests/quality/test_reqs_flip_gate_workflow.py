"""The CI wiring the reqs flip gate needs to be a gate at all (issue #1605).

`scripts/reqs_flip_gate.py` has its own unit tests. This module tests the two
properties of `ci.yml` that decide whether that script's verdict is worth
anything, both of which were defects on PR #1709's first head and neither of
which any script-level test can see.

Requirements:
- ✔︎ ✅ 🎯 The gate re-runs when PR metadata changes, not only when the head moves.
- ✔︎ ✅ 🎯 The job running the gate can actually read the pull request.

Acceptance tests:
- [if] `ci.yml`'s `pull_request` types omit `edited` [then] this fails
  [⛔️ if an author can pass the gate and then edit the reason out of the body
  with no new head SHA, leaving the green check standing at merge time].
- [if] the types list drops one of GitHub's three defaults [then] this fails
  [⛔️ if naming `types` at all silently narrows when CI runs].
- [if] the `contracts` job stops declaring `pull-requests: read` [then] this
  fails [⛔️ if `gh pr view` goes back to "Resource not accessible by
  integration" and the gate can only report UNKNOWN].
- [if] that job declares permissions without `contents: read` [then] this fails
  [⛔️ if naming a block replaces the default and breaks actions/checkout].

-Claude
"""

from __future__ import annotations

from pathlib import Path

import yaml

CI = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"

# GitHub's implicit `pull_request` type list. Naming `types` replaces it, so a
# regression here is a NARROWING, not just a missing entry.
GITHUB_DEFAULT_TYPES = {"opened", "synchronize", "reopened"}

GATE_STEP_MARKER = "scripts.reqs_flip_gate"


def _ci() -> dict:
    # PyYAML reads the bare `on:` key as the boolean True, not the string "on",
    # so callers ask for both keys rather than relying on one of them existing.
    return yaml.safe_load(CI.read_text())


def test_the_gate_reruns_when_pr_metadata_is_edited() -> None:
    doc = _ci()
    triggers = doc.get("on", doc.get(True))
    types = set(triggers["pull_request"]["types"])

    assert "edited" in types, (
        "ci.yml's pull_request trigger must include `edited`: the reqs flip "
        "gate reads the PR title and body, which change with no new head SHA, "
        "so without it a passing check outlives the metadata that earned it"
    )
    missing = GITHUB_DEFAULT_TYPES - types
    assert not missing, (
        f"naming `types` replaces GitHub's defaults, and {sorted(missing)} "
        "would no longer trigger CI at all"
    )


def test_the_job_running_the_gate_can_read_the_pull_request() -> None:
    doc = _ci()
    contracts = doc["jobs"]["contracts"]

    steps = contracts.get("steps")
    assert steps is not None, "the contracts job has no steps to inspect"
    assert any(GATE_STEP_MARKER in str(step.get("run", "")) for step in steps), (
        f"no step in the contracts job runs {GATE_STEP_MARKER}; if the gate "
        "moved, move this contract with it rather than deleting it"
    )

    permissions = contracts.get("permissions")
    assert permissions is not None, (
        "the contracts job needs an explicit permissions block: this "
        "repository's default workflow permissions are `read`, which covers "
        "contents and packages only, so `gh pr view` cannot reach the API"
    )
    assert permissions.get("pull-requests") == "read", (
        "the contracts job must declare `pull-requests: read` or the gate can "
        "only ever report UNKNOWN (exit 2), which reddens CI on every PR"
    )
    assert permissions.get("contents") == "read", (
        "naming a permissions block REPLACES the default for the job, so "
        "`contents: read` has to be restated or actions/checkout loses access"
    )
