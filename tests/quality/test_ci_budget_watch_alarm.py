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

import json
import os
import re
import shutil
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest
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


# ----- issue #3166: a ledger that cannot measure must not go silent ----------------
#
# The ledger step's own failure (most often a credential that lost billing-read
# scope) exited the job non-zero with no `continue-on-error`, so GitHub skipped
# both alarm steps above -- 26 consecutive scheduled runs, 11 days, no one told.
# These tests pin the fix's shape, not its exact wording: the ledger step must
# survive its own failure, the alarm-issue step must reuse ONE code path to also
# fire on that failure, and something must still keep the job red.

LEDGER_STEP = "Build the spend ledger"
ALERT_STEP = "Open or update the budget alert issue"


def _watch_steps(workflow: dict) -> list[dict]:
    return workflow["jobs"]["watch"]["steps"]


def _step(workflow: dict, name: str) -> dict:
    return next(step for step in _watch_steps(workflow) if step.get("name") == name)


def alert_issue_step_fires_on_ledger_failure(workflow: dict) -> bool:
    """Return whether the alert-issue step's own condition covers a blind ledger."""
    condition = _step(workflow, ALERT_STEP).get("if") or ""
    return "steps.ledger.outcome" in condition and "failure" in condition


def alert_issue_step_fires_on_unknown_state(workflow: dict) -> bool:
    """Return whether the alert-issue step also covers the ledger's own UNKNOWN verdict.

    UNKNOWN (DEVOPS-11: billing data missing or stale) is a second, quieter way
    the ledger can fail to measure -- it exits 0 with no WARN/STOP, so it was
    silent for the same reason the outright crash was.
    """
    condition = _step(workflow, ALERT_STEP).get("if") or ""
    return "steps.ledger.outputs.state == 'UNKNOWN'" in condition


def a_failed_ledger_still_fails_the_job(workflow: dict) -> bool:
    """Return whether some step between the ledger and the sentinel reds the job
    on either way the ledger can fail to measure: outright failure, or UNKNOWN."""
    steps = _watch_steps(workflow)
    names = [step.get("name") for step in steps]
    ledger_index = names.index(LEDGER_STEP)
    sentinel_index = names.index(SENTINEL_STEP)
    return any(
        "steps.ledger.outcome" in (step.get("if") or "")
        and "failure" in (step.get("if") or "")
        and "steps.ledger.outputs.state == 'UNKNOWN'" in (step.get("if") or "")
        and "exit 1" in (step.get("run") or "")
        for step in steps[ledger_index:sentinel_index]
    )


# REQ: DEVOPS-13
def test_the_ledger_step_survives_its_own_failure() -> None:
    """A broken credential must not take the alarm steps down with it."""
    ledger = _step(_workflow(), LEDGER_STEP)
    assert ledger.get("continue-on-error") is True, (
        "the ledger step has no continue-on-error, so a failed credential skips "
        "every step after it -- including the alarm that exists to report that "
        "exact failure (#3166)"
    )


# REQ: DEVOPS-13
def test_the_alert_issue_step_also_fires_on_a_ledger_failure() -> None:
    """WARN, STOP, and a failed ledger reuse the same alert-issue step."""
    workflow = _workflow()
    assert alert_issue_step_fires_on_ledger_failure(workflow), (
        f"{ALERT_STEP!r} does not gate on steps.ledger.outcome == 'failure', so a "
        "broken credential opens no alert at all"
    )
    condition = _step(workflow, ALERT_STEP)["if"]
    for state in ("WARN", "STOP"):
        assert f"steps.ledger.outputs.state == '{state}'" in condition, (
            f"the {state} alarm path must survive the #3166 fix unweakened; "
            f"got if: {condition!r}"
        )


# REQ: DEVOPS-13
def test_a_ledger_failure_still_keeps_the_job_red() -> None:
    """continue-on-error on the ledger step must not let a blind run go green."""
    assert a_failed_ledger_still_fails_the_job(_workflow()), (
        "no step between the ledger and the sentinel fails the job on "
        "steps.ledger.outcome == 'failure', so continue-on-error alone would "
        "let a blind run report success -- worse silence than a red run (#3166)"
    )


# REQ: DEVOPS-13
def test_the_alert_issue_step_also_fires_on_an_unknown_state() -> None:
    """The ledger's own UNKNOWN verdict is the same 'cannot measure' bug."""
    assert alert_issue_step_fires_on_unknown_state(_workflow()), (
        f"{ALERT_STEP!r} does not gate on steps.ledger.outputs.state == 'UNKNOWN', "
        "so a stale or missing billing feed exits 0 with no WARN/STOP and no alert"
    )


def test_reverting_the_alert_condition_to_pre_fix_is_rejected() -> None:
    """Mutation: put the WARN/STOP-only condition back and the checker must catch it."""
    mutated = deepcopy(_workflow())
    _step(mutated, ALERT_STEP)["if"] = (
        "steps.ledger.outputs.state == 'WARN' || steps.ledger.outputs.state == 'STOP'"
    )
    assert alert_issue_step_fires_on_ledger_failure(mutated) is False


def test_removing_the_ledger_fail_step_is_rejected() -> None:
    """Mutation: drop the step that reds the job on a blind ledger and the checker must catch it."""
    mutated = deepcopy(_workflow())
    steps = mutated["jobs"]["watch"]["steps"]
    steps[:] = [
        step
        for step in steps
        if step.get("name") != "Fail the job when the ledger cannot measure spend"
    ]
    assert a_failed_ledger_still_fails_the_job(mutated) is False


def test_dropping_unknown_from_the_alert_condition_is_rejected() -> None:
    """Mutation: narrow the alert condition back to just the crash, drop UNKNOWN."""
    mutated = deepcopy(_workflow())
    _step(mutated, ALERT_STEP)["if"] = (
        "steps.ledger.outcome == 'failure' || "
        "steps.ledger.outputs.state == 'WARN' || "
        "steps.ledger.outputs.state == 'STOP'"
    )
    assert alert_issue_step_fires_on_unknown_state(mutated) is False


def test_dropping_unknown_from_the_fail_step_is_rejected() -> None:
    """Mutation: narrow the job-reddening condition back to just the crash."""
    mutated = deepcopy(_workflow())
    steps = mutated["jobs"]["watch"]["steps"]
    for step in steps:
        if step.get("name") == "Fail the job when the ledger cannot measure spend":
            step["if"] = "steps.ledger.outcome == 'failure'"
    assert a_failed_ledger_still_fails_the_job(mutated) is False


# ----- functional: the shipped bash, run for real against a stubbed `gh` ----------
#
# Found live against the real workflow while verifying #3166: dispatching with
# `month: junk` put "junk" straight into a GitHub issue title and opened a
# stray issue (#3211) instead of updating the month's real one, because MONTH
# fell back to the raw, unvalidated workflow_dispatch input the moment the
# ledger crashed before resolving its own. These tests run the actual shipped
# script, not a description of it, against a stub `gh` that only records the
# title it was asked to create.

_GH_STUB = """#!/usr/bin/env bash
set -euo pipefail
case "$1 $2" in
  "issue list")
    echo -n ""
    ;;
  "label create")
    ;;
  "issue create")
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --title) printf '%s' "$2" > "$TITLE_OUT"; shift 2 ;;
        *) shift ;;
      esac
    done
    ;;
  *)
    echo "unhandled mock gh invocation: $*" >&2
    exit 1
    ;;
esac
"""


def _run_alert_script_for_title(tmp_path: Path, month_input: str) -> str:
    """Run the shipped alert-issue step's bash for real; return the title it created."""
    script = _step(_workflow(), ALERT_STEP)["run"]
    gh_stub = tmp_path / "gh"
    gh_stub.write_text(_GH_STUB)
    gh_stub.chmod(0o755)
    title_out = tmp_path / "title.txt"
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "TITLE_OUT": str(title_out),
        "GH_TOKEN": "x",
        "LEDGER_OUTCOME": "failure",
        "STATE": "",
        "PCT": "",
        "USED": "",
        "LIMIT": "",
        "MONTH": "",
        "MONTH_INPUT": month_input,
    }
    subprocess.run(["bash", "-c", script], check=True, cwd=tmp_path, env=env, timeout=30)
    return title_out.read_text()


def test_a_garbage_month_input_never_reaches_the_issue_title(tmp_path: Path) -> None:
    """`month: junk` must resolve to a real UTC month, never the raw operator typo."""
    title = _run_alert_script_for_title(tmp_path, "junk")
    assert "junk" not in title, f"the raw dispatch input leaked into the issue title: {title!r}"
    assert re.fullmatch(r"CI budget BLIND: \d{4}-\d{2} .+", title), (
        f"expected a real YYYY-MM month in the title; got {title!r}"
    )


def test_a_valid_month_input_is_still_honored(tmp_path: Path) -> None:
    """A well-formed YYYY-MM dispatch input is real operator intent, not noise -- keep it."""
    title = _run_alert_script_for_title(tmp_path, "2026-07")
    assert "2026-07" in title, f"a valid month input should be honored; got {title!r}"


# ----- functional: the streak's jq reduce, run for real against a fixed history --
#
# The reduce lives entirely inside a --jq expression, invisible to the title/month
# tests above (they always start from an issue with no history, so `existing` is
# empty and the reduce never runs). This exercises it directly against a comment
# history that mixes a WARN comment in among BLIND ones, which is the case the
# reduce exists to get right: a human or a measured month breaks the streak, an
# unbroken run of BLIND markers does not.

_GH_STUB_WITH_HISTORY = """#!/usr/bin/env bash
set -euo pipefail
case "$1 $2" in
  "issue list")
    printf '%s' "$EXISTING_ISSUE"
    ;;
  "issue view")
    # args end with: ... --json comments --jq '<expr>' -- take the last one.
    jq "${!#}" "$COMMENTS_JSON"
    ;;
  "issue comment")
    cp "$5" "$COMMENT_OUT"
    ;;
  *)
    echo "unhandled mock gh invocation: $*" >&2
    exit 1
    ;;
esac
"""


@pytest.mark.skipif(
    shutil.which("jq") is None,
    reason="exercises the alert step's own jq reduce expression against real jq",
)
def test_the_streak_count_breaks_on_a_non_blind_comment(tmp_path: Path) -> None:
    """A WARN comment sitting between BLIND comments must stop the count there,
    so the oldest BLIND comment (behind the WARN one) is never reached."""
    comments = [
        {"body": "<!-- ci-budget-watch:blind -->oldest blind report, unreachable"},
        {"body": "CI budget WARN: 2026-07 at 72% of included minutes"},
        {"body": "<!-- ci-budget-watch:blind -->second blind report"},
        {"body": "<!-- ci-budget-watch:blind -->third blind report, newest"},
    ]
    comments_json = tmp_path / "comments.json"
    comments_json.write_text(json.dumps({"comments": comments}))
    comment_out = tmp_path / "comment.md"

    script = _step(_workflow(), ALERT_STEP)["run"]
    gh_stub = tmp_path / "gh"
    gh_stub.write_text(_GH_STUB_WITH_HISTORY)
    gh_stub.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "EXISTING_ISSUE": "692",
        "COMMENTS_JSON": str(comments_json),
        "COMMENT_OUT": str(comment_out),
        "GH_TOKEN": "x",
        "LEDGER_OUTCOME": "failure",
        "STATE": "",
        "PCT": "",
        "USED": "",
        "LIMIT": "",
        "MONTH": "2026-07",
        "MONTH_INPUT": "",
    }
    subprocess.run(["bash", "-c", script], check=True, cwd=tmp_path, env=env, timeout=30)

    body = comment_out.read_text()
    # Newest-first: third (blind, count=1), second (blind, count=2), WARN (stop).
    # The oldest blind comment sits behind the WARN one and must not be counted.
    assert "3 consecutive blind report(s)" in body, body
