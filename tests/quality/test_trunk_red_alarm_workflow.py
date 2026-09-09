"""Issues #1155 and #1309: failed push-to-main checks must create a loud trunk alarm.

The quality ratchet evaluates the actual merge result only after it has landed.
Without a separate workflow-run watchdog, a normal failed CI conclusion stays in
the Actions tab until PR authors notice their unrelated failures. This test reads
the shipped workflow, not a second hand-maintained representation, and pins the
only event that matters: a CI run triggered by a push to the default branch that
concludes ``failure``.

Regression lines:
  - if a push-to-main CI or E2E failure cannot reach the alarm job then broken
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
    # `schedule` joined `push` for issue #1504: the nightly extended tier had
    # been red for eight nights with no alarm because only pushes were admitted.
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
        step for step in steps if step["name"] == "Open or update trunk-red issue and fail visibly"
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
