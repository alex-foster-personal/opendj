"""The `quality` ratchet job prefers CI_RUNS_ON_QUALITY, off cold-cache megamac VMs.

Tue 6 Oct 2026 (the ADR-0041 priority-pool lever applied to one job): ci.yml's
`quality` job ran on CI_RUNS_ON_LINUX, which the megamac-vm-* runners match, and
its "mypy cache acceptance (slow)" step hit its 15-minute timeout twice on PR
#5482 (megamac-vm-3, megamac-vm-4) from a cold mypy cache per VM; the same step
takes 2-7 min on agbox/nucbox. The existing repo variable CI_RUNS_ON_QUALITY
(labels self-hosted, linux, linux-light, pytest) excludes megamac, which carries
no `pytest` label. The job now reads it after the Trunk-draft merge-queue
disjunct and before the light pool.

Checked as behavior with the evaluator in test_ci_trunk_agentbox_pool.py, with a
control that the pre-change expression never resolves to CI_RUNS_ON_QUALITY.

Regression lines:
  - if a set CI_RUNS_ON_QUALITY does not route quality there on a PR, a main push
    or a dispatch then the mypy step lands on cold-cache megamac VMs again
  - if an unset CI_RUNS_ON_QUALITY resolves differently from before for any event
    then rollback is not a variable delete
  - if a Trunk draft does not take CI_RUNS_ON_MERGE_QUEUE first then batches queue
    behind PR CI
  - if any other ci.yml job reads CI_RUNS_ON_QUALITY then the pool widened silently
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from tests.scripts.ci_runner_routes import LIGHT_RUNS_ON, MAIN_QUALITY_RUNS_ON, QUALITY_RUNS_ON, TRUNK_APP_LOGIN
from tests.scripts.test_ci_trunk_agentbox_pool import LIVE_VARS, Event, resolve

CI = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"

QUALITY = '["self-hosted","linux","linux-light","pytest"]'
MERGE_QUEUE = '["self-hosted","linux","mq"]'
UNSET = {**LIVE_VARS, "CI_RUNS_ON_MERGE_QUEUE": MERGE_QUEUE}
SET = {**UNSET, "CI_RUNS_ON_QUALITY": QUALITY}

#: Today's routing, before this change: the control.
PRE_CHANGE_QUALITY = LIGHT_RUNS_ON

NON_DRAFT_EVENTS = {
    "main-push": Event("push", "refs/heads/main", variables=SET),
    "plain-pr": Event("pull_request", "refs/pull/2/merge", ("area:ci",), SET, "af--feature"),
    "repair-pr": Event("pull_request", "refs/pull/1/merge", ("ci:trunk-repair",), SET, "af--fix"),
    "branch-push": Event("push", "refs/heads/af--feature", variables=SET),
    "main-dispatch": Event("workflow_dispatch", "refs/heads/main", variables=SET),
}
TRUNK_DRAFT = Event(
    "pull_request",
    "refs/pull/3/merge",
    variables=SET,
    head_ref="trunk-merge/pr-5482/abc",
    author=TRUNK_APP_LOGIN,
)
EVERY_EVENT = {**NON_DRAFT_EVENTS, "trunk-draft": TRUNK_DRAFT}


def _runs_on(job_id: str) -> str:
    return str(yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"][job_id]["runs-on"])


def _unset(event: Event) -> Event:
    return replace(event, variables=UNSET)


def test_quality_carries_the_named_route() -> None:
    """if the quality runs-on drifts from QUALITY_RUNS_ON then the cases below test another expression"""
    # Wed 4 Nov 2026: a push to main prefers CI_RUNS_ON_MAIN first; the chain after it
    # is QUALITY_RUNS_ON, which the cases below evaluate.
    assert _runs_on("quality") == MAIN_QUALITY_RUNS_ON


@pytest.mark.parametrize("name", sorted(EVERY_EVENT))
def test_control_pre_change_expression_never_reaches_the_quality_pool(name: str) -> None:
    """if the pre-change expression resolves to CI_RUNS_ON_QUALITY then the evaluator cannot tell the routes apart"""
    got = resolve(PRE_CHANGE_QUALITY, EVERY_EVENT[name])
    assert got != json.loads(QUALITY)
    expected = MERGE_QUEUE if name == "trunk-draft" else LIVE_VARS["CI_RUNS_ON_LINUX"]
    assert got == json.loads(expected), (name, got)


@pytest.mark.parametrize("name", sorted(NON_DRAFT_EVENTS))
def test_set_variable_routes_quality_to_the_quality_pool(name: str) -> None:
    """if a set CI_RUNS_ON_QUALITY does not win for a non-draft event then megamac keeps the mypy step"""
    assert resolve(QUALITY_RUNS_ON, NON_DRAFT_EVENTS[name]) == json.loads(QUALITY)


@pytest.mark.parametrize("name", sorted(EVERY_EVENT))
def test_unset_variable_resolves_exactly_as_before(name: str) -> None:
    """if an unset CI_RUNS_ON_QUALITY changes any event's routing then rollback is not a variable delete"""
    event = _unset(EVERY_EVENT[name])
    assert resolve(QUALITY_RUNS_ON, event) == resolve(PRE_CHANGE_QUALITY, event)
    bare = replace(event, variables={})
    assert resolve(QUALITY_RUNS_ON, bare) == "ubuntu-latest"


def test_trunk_draft_takes_the_merge_queue_pool_first() -> None:
    """if a Trunk draft prefers CI_RUNS_ON_QUALITY over CI_RUNS_ON_MERGE_QUEUE then batches queue behind PRs"""
    assert resolve(QUALITY_RUNS_ON, TRUNK_DRAFT) == json.loads(MERGE_QUEUE)
    no_mq = replace(TRUNK_DRAFT, variables={k: v for k, v in SET.items() if k != "CI_RUNS_ON_MERGE_QUEUE"})
    assert resolve(QUALITY_RUNS_ON, no_mq) == json.loads(QUALITY)


def test_only_the_quality_job_reads_the_quality_variable() -> None:
    """if another ci.yml job reads CI_RUNS_ON_QUALITY then the pool widened without a decision"""
    jobs = yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]
    readers = {j for j, job in jobs.items() if "CI_RUNS_ON_QUALITY" in str(job.get("runs-on", ""))}
    assert readers == {"quality"}, readers
