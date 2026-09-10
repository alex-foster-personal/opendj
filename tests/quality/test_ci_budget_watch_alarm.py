"""Issue #1740: the spend alarm must survive the coverage sentinel's verdict.

`ci-budget-watch.yml` carries the month-to-date CI spend ledger and two alarm
steps: it opens or updates the budget issue, and it fails the job once spend is
over the stop threshold. Both sat *below* the `PR head CI coverage` sentinel in
the same job, so when the sentinel failed -- which it did on every run from
Tue 8 Sep 2026, because thirteen merge-conflicted PRs can never produce an
Actions run at their head -- GitHub skipped every later step in the job. Nobody
was told the spend number for two days, and the over-threshold stop never
evaluated once in that window.

The defect is structural, not the one ordering: a step that FAILS takes every
later step of its job with it unless that step carries an `always()` condition.
So this test reads the shipped workflow and pins the one property that matters:
the sentinel's failure cannot be what stops an alarm step from running. Any of
the three honest shapes satisfies it -- run the sentinel in another job, run it
after the alarms, or make the alarm steps unconditional. What it rejects is the
sentinel sitting above an alarm step in the same job with a conditional `if`,
which is exactly the arrangement that held the spend alarm down.

The mirror of that coupling is pinned too, because the obvious fix creates it:
once the sentinel is last, the STOP step above it can exit 1 over the threshold
and GitHub's implicit success guard skips the sentinel in turn, so every
over-threshold run would publish no coverage status. The sentinel has to survive
an earlier step's failure.

Equally pinned, because the cheap wrong fix is to silence the sentinel instead:
the sentinel must still be able to fail the run.

Regression lines:
  - if a failing coverage sentinel can make a spend alarm step be skipped then broken
  - if a failing step above the sentinel can make the sentinel be skipped then broken
  - if moving the sentinel above the alarm steps stops being rejected then broken
  - if the coverage sentinel can no longer fail the run then broken
  - if either alarm step disappears from the budget workflow then broken
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github" / "workflows" / "ci-budget-watch.yml"

# The step the sentinel owns, and the two steps it was suppressing.
SENTINEL_STEP = "Refuse untested non-docs pull request heads"
ALARM_STEPS = (
    "Open or update the budget alert issue",
    "Fail the job when month-to-date is over the stop threshold",
)

# Step conditions that survive an earlier step's failure. `always()` also runs
# on a concurrency-superseded run; `!cancelled()` is the narrower form used by
# the sentinel. Anything else is evaluated only after a clean run so far, which
# is the property under test.
SURVIVES_FAILURE = ("always()", "!cancelled()")


def _workflow() -> dict:
    """Load the actual GitHub workflow that owns the spend ledger."""
    assert WORKFLOW.is_file(), (
        "the CI budget watch workflow is missing, so nothing reports month-to-date "
        "Actions spend or stops the month at its threshold"
    )
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(document, dict), "the CI budget watch workflow is not a mapping"
    return document


# ----- the invariant ---------------------------------------------------------------


def alarm_steps_the_sentinel_can_suppress(workflow: dict) -> list[str]:
    """Return the alarm steps a failing sentinel would skip to.

    GitHub runs a job's steps in order and skips the rest of them the moment one
    fails, unless that later step carries an ``always()`` condition. An alarm
    step therefore survives the sentinel when it is in a different job, when it
    precedes the sentinel in its own job, or when it is unconditional.
    """
    suppressed: list[str] = []
    for job in workflow.get("jobs", {}).values():
        steps = job.get("steps") or []
        names = [step.get("name") for step in steps]
        if SENTINEL_STEP not in names:
            continue
        sentinel_index = names.index(SENTINEL_STEP)
        for alarm in ALARM_STEPS:
            if alarm not in names or names.index(alarm) < sentinel_index:
                continue
            condition = steps[names.index(alarm)].get("if") or ""
            if not any(survives in condition for survives in SURVIVES_FAILURE):
                suppressed.append(alarm)
    return suppressed


def the_sentinel_itself_can_be_suppressed(workflow: dict) -> bool:
    """Return whether a failing step above the sentinel would skip the sentinel.

    The mirror of ``alarm_steps_the_sentinel_can_suppress``. Once the sentinel
    is the last step, the STOP step above it exits 1 over the threshold, and
    GitHub's implicit success guard skips the sentinel unless its own condition
    survives an earlier failure.
    """
    for job in workflow.get("jobs", {}).values():
        steps = job.get("steps") or []
        names = [step.get("name") for step in steps]
        if SENTINEL_STEP not in names:
            continue
        index = names.index(SENTINEL_STEP)
        condition = steps[index].get("if") or ""
        if any(survives in condition for survives in SURVIVES_FAILURE):
            return False
        # Nothing above it means nothing above it can fail into it.
        return index > 0
    return False


# ----- the workflow as shipped -----------------------------------------------------


def test_the_coverage_sentinel_cannot_suppress_the_spend_alarm() -> None:
    """A red sentinel must not be what stops the ledger's alarm steps running."""
    suppressed = alarm_steps_the_sentinel_can_suppress(_workflow())
    assert suppressed == [], (
        f"the coverage sentinel can suppress {suppressed}: GitHub skips every later "
        "step of a job once a step fails, so one open PR head with no Actions run "
        "silences the month-to-date spend alarm and the over-threshold stop"
    )


def test_the_over_threshold_stop_cannot_suppress_the_coverage_sentinel() -> None:
    """The STOP step failing must not skip the sentinel that now sits below it."""
    assert the_sentinel_itself_can_be_suppressed(_workflow()) is False, (
        "the sentinel sits below the STOP step with no condition surviving an "
        "earlier failure, so every run at or above 90% of the allowance would "
        "publish no coverage status at all"
    )


def test_both_alarm_steps_are_present() -> None:
    """Neither alarm step may be deleted to satisfy the ordering property."""
    workflow = _workflow()
    names = [
        step.get("name")
        for job in workflow.get("jobs", {}).values()
        for step in (job.get("steps") or [])
    ]
    for alarm in ALARM_STEPS:
        assert alarm in names, (
            f"{alarm!r} is missing from ci-budget-watch.yml; the ledger must report "
            "spend and fail loudly at the stop threshold"
        )


def test_the_coverage_sentinel_can_still_fail_the_run() -> None:
    """The coverage finding stays loud: decoupling it must not silence it."""
    workflow = _workflow()
    sentinel = next(
        step
        for job in workflow.get("jobs", {}).values()
        for step in (job.get("steps") or [])
        if step.get("name") == SENTINEL_STEP
    )
    assert sentinel.get("continue-on-error") is not True, (
        "the coverage sentinel is marked continue-on-error, so an uncovered PR head "
        "is reported and then discarded"
    )
    command = sentinel.get("run") or ""
    assert command.strip().endswith("--publish-status"), (
        f"the sentinel must run the coverage gate itself and inherit its exit code; got {command!r}"
    )
    for swallowed in ("|| true", "|| echo", "exit 0"):
        assert swallowed not in command, (
            f"the sentinel's exit code is swallowed by {swallowed!r}, so it can no "
            "longer fail the run"
        )


# ----- mutation: the exact pre-#1740 arrangement -----------------------------------


def test_restoring_the_sentinel_above_the_alarm_steps_is_rejected() -> None:
    """The bug's own shape: sentinel first, alarm steps after it, both conditional."""
    mutated = deepcopy(_workflow())
    jobs = mutated["jobs"]
    for job in jobs.values():
        steps = job.get("steps") or []
        names = [step.get("name") for step in steps]
        if SENTINEL_STEP not in names or names[-1] != SENTINEL_STEP:
            continue
        sentinel = steps.pop(names.index(SENTINEL_STEP))
        steps.insert(0, sentinel)

    suppressed = alarm_steps_the_sentinel_can_suppress(mutated)
    assert set(suppressed) == set(ALARM_STEPS), (
        "planting the sentinel above both alarm steps must be caught; "
        f"the check reported {suppressed}"
    )


def test_dropping_the_sentinels_own_guard_is_rejected() -> None:
    """The sentinel last, but back on the implicit success guard, is the mirror bug."""
    mutated = deepcopy(_workflow())
    for job in mutated["jobs"].values():
        for step in job.get("steps") or []:
            if step.get("name") == SENTINEL_STEP:
                step.pop("if", None)

    assert the_sentinel_itself_can_be_suppressed(mutated) is True, (
        "removing the sentinel's guard must be caught: without it the STOP step "
        "skips the sentinel on every over-threshold run"
    )
