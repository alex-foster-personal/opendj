"""Issues #1155, #1309 and #1504: failed trunk checks must create a loud alarm.

The quality ratchet evaluates the actual merge result only after it has landed.
Without a separate workflow-run watchdog, a normal failed CI conclusion stays in
the Actions tab until PR authors notice their unrelated failures. This test reads
the shipped workflow, not a second hand-maintained representation, and pins the
two events that matter: a CI or E2E run triggered by a push to the default
branch, and a scheduled E2E run (E2E's `extended` job runs ONLY on schedule or
workflow_dispatch, never push, so a push-only filter can never see it fail --
issue #1504, the nightly extended tier failed 8 nights running with no alarm).
Both must conclude ``failure`` on the default branch to fire.

Regression lines:
  - if a push-to-main CI or E2E failure cannot reach the alarm job then broken
  - if a scheduled E2E failure cannot reach the alarm job then broken
  - if the alarm job does not open or update the durable trunk-red issue then broken
  - if the alarm job can report success after it records a red run then broken
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github" / "workflows" / "trunk-red-alarm.yml"


def _workflow() -> dict:
    """Load the actual GitHub workflow that owns the trunk-red alarm."""
    assert WORKFLOW.is_file(), (
        "the trunk-red alarm workflow is missing, so a push-to-main CI failure "
        "has no durable alert outside the failed run itself"
    )
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(document, dict), "the trunk-red alarm workflow is not a mapping"
    return document


def _assert_watched_trunk_workflows(workflow: dict) -> None:
    """Reject a watchdog definition that leaves a trunk check unwatched."""
    trigger = workflow[True]["workflow_run"]
    assert trigger["workflows"] == ["CI", "E2E"]


def test_a_planted_failed_trunk_workflow_push_can_reach_the_trunk_red_alarm() -> None:
    """If CI or E2E fails after a main push then the separate alarm must fail."""
    workflow = _workflow()
    _assert_watched_trunk_workflows(workflow)
    trigger = workflow[True]["workflow_run"]
    assert trigger["types"] == ["completed"]

    job = workflow["jobs"]["trunk_red"]
    condition = " ".join(job["if"].split())
    expected = (
        "(github.event.workflow_run.event == 'push' "
        "|| github.event.workflow_run.event == 'schedule') "
        "&& github.event.workflow_run.head_branch "
        "== github.event.repository.default_branch "
        "&& github.event.workflow_run.conclusion == 'failure'"
    )
    assert condition == expected, (
        "the alarm must admit exactly a failed watched-workflow push or scheduled run "
        f"on the default branch; got {condition!r}"
    )

    steps = job["steps"]
    alarm = next(
        step
        for step in steps
        if step["name"] == "Open or update trunk-red issue and fail visibly"
    )
    command = alarm["run"]
    assert "gh issue create" in command and "gh issue comment" in command, (
        "the alarm must create or update one durable tracking issue, not leave "
        "the failure only in ephemeral Actions logs"
    )
    assert "exit 1" in command, (
        "the watchdog itself must go red after recording a trunk failure, or it "
        "cannot be used as a planted-failure proof"
    )
    assert alarm["env"]["WORKFLOW_NAME"] == "${{ github.event.workflow_run.name }}", (
        "the shared alert must name CI or E2E so failures on one SHA remain distinct evidence"
    )
    assert "- Workflow: \\`$WORKFLOW_NAME\\`" in command, (
        "an open incident may aggregate runs, but every appended report must retain its workflow"
    )


def test_removing_e2e_from_the_alarm_trigger_fails_the_mutation_check() -> None:
    """A red E2E cannot silently fall out of the alarm's subscription."""
    mutated = deepcopy(_workflow())
    mutated[True]["workflow_run"]["workflows"] = ["CI"]

    try:
        _assert_watched_trunk_workflows(mutated)
    except AssertionError:
        return
    raise AssertionError("removing E2E must make the watchdog contract fail")


def test_narrowing_the_alarm_back_to_push_only_fails_the_mutation_check() -> None:
    """Issue #1504: a scheduled E2E failure must not silently stop reaching the alarm.

    E2E's `extended` job runs ONLY on schedule/workflow_dispatch, never push, so
    reverting the `if:` guard to push-only re-opens the exact blind spot that let
    the nightly extended tier fail 8 nights running with no human-visible alert.
    """
    workflow = _workflow()
    job = workflow["jobs"]["trunk_red"]
    condition = " ".join(job["if"].split())
    assert "github.event.workflow_run.event == 'schedule'" in condition, (
        "the alarm's if-condition must admit a scheduled trunk-watched workflow run; "
        f"got {condition!r}"
    )

    push_only = (
        "github.event.workflow_run.event == 'push' "
        "&& github.event.workflow_run.head_branch "
        "== github.event.repository.default_branch "
        "&& github.event.workflow_run.conclusion == 'failure'"
    )
    assert "schedule" in condition and condition != push_only, (
        "a push-only condition would leave every scheduled E2E failure unalarmed "
        "(issue #1504)"
    )
