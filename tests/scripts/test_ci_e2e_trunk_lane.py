"""Main's own E2E (a push to main, and only that) prefers the e2e-trunk runner lane.

ADR-NEW-main-e2e-gets-a-trunk-runner-lane (Mon 5 Oct 2026, the ADR-0041
priority-pool lever applied to the e2e job class). That evening main's E2E for
1474db5540 (run 37343359337) sat queued 90+ minutes behind about 30 PR E2E jobs
on the 4 `e2e` runners: GitHub does not hand queued jobs to a free runner in
FIFO order. The nucbox trunk checkpoint needs `e2e verdict` on a main SHA to
clear RED, so it could not clear, and the admission reconciler held every PR.

e2e.yml's `gate` job prefers CI_RUNS_ON_E2E_TRUNK (label `e2e-trunk`) for a
push to main. Every e2e runner carries `e2e-trunk` and one carries only it, so
main has a runner the PR backlog cannot take and can still use the shared ones;
that is host config, recorded in fleet-af.

Scope, restated Fri 9 Oct 2026: this lane is stacked on the e2e trunk-repair
reserve (ADR-NEW-e2e-trunk-repair-runner-reserve, pinned by
test_ci_e2e_main_fix_runner_reserve.py). A `ci:trunk-repair` pull request
belongs to that reserve and its variable CI_RUNS_ON_E2E_MAIN_FIX, never to this
lane, mirroring ci.yml where CI_RUNS_ON_TRUNK and CI_RUNS_ON_MAIN_FIX are
separate variables. The lane is one disjunct in front of the reserve's.

The routing is checked as behavior with the evaluator in
test_ci_trunk_agentbox_pool.py, and its semantics by two controls: the bare
pool expression must resolve every event to CI_RUNS_ON_E2E, and the reserve
alone must send a repair PR, and nothing else, to CI_RUNS_ON_E2E_MAIN_FIX.

Regression lines:
  - if a main push does not resolve to CI_RUNS_ON_E2E_TRUNK when it is set then main's
    E2E queues behind the PR backlog and the checkpoint cannot clear
  - if any pull request (a ci:trunk-repair one included), a non-main push, a tag push,
    a schedule or a dispatch resolves to CI_RUNS_ON_E2E_TRUNK then that load can take
    the trunk-only runner
  - if a ci:trunk-repair PR stops resolving to CI_RUNS_ON_E2E_MAIN_FIX then this lane
    has broken the reserve it is stacked on
  - if an unset CI_RUNS_ON_E2E_TRUNK does not resolve exactly as the reserve alone does
    then rollback is not a variable unset
  - if the nightly extended job, or any other job, reads CI_RUNS_ON_E2E_TRUNK then it
    can hold the trunk-only runner for hours
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from tests.scripts.ci_runner_routes import (
    E2E_GATE_RUNS_ON,
    E2E_MAIN_FIX_DISJUNCT,
    E2E_MERGE_QUEUE_DISJUNCT,
    E2E_POOL_RUNS_ON,
    E2E_REPAIR_RESERVE_RUNS_ON,
    E2E_TRUNK_DISJUNCT,
)
from tests.scripts.test_ci_trunk_agentbox_pool import Event, resolve

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
E2E_WORKFLOW = WORKFLOWS / "e2e.yml"

VARIABLE = "CI_RUNS_ON_E2E_TRUNK"
RESERVE_VARIABLE = "CI_RUNS_ON_E2E_MAIN_FIX"
E2E = '["self-hosted","linux","e2e"]'
E2E_TRUNK = '["self-hosted","linux","e2e-trunk"]'
E2E_MAIN_FIX = '["self-hosted","linux","e2e-main-fix"]'
POOL_LABELS = json.loads(E2E)
TRUNK_LABELS = json.loads(E2E_TRUNK)
RESERVE_LABELS = json.loads(E2E_MAIN_FIX)

#: Every combination of the two opt-in variables; the pool variable is always set.
BOTH_SET = {"CI_RUNS_ON_E2E": E2E, VARIABLE: E2E_TRUNK, RESERVE_VARIABLE: E2E_MAIN_FIX}
TRUNK_ONLY = {"CI_RUNS_ON_E2E": E2E, VARIABLE: E2E_TRUNK}
RESERVE_ONLY = {"CI_RUNS_ON_E2E": E2E, RESERVE_VARIABLE: E2E_MAIN_FIX}
NEITHER = {"CI_RUNS_ON_E2E": E2E}
TRUNK_SET = {"both set": BOTH_SET, "trunk only": TRUNK_ONLY}
TRUNK_UNSET = {"reserve only": RESERVE_ONLY, "neither": NEITHER}

MAIN_PUSH = Event("push", "refs/heads/main")
REPAIR_PR = Event("pull_request", "refs/pull/1/merge", ("ci:trunk-repair",), head_ref="af--fix")
#: Everything that is not a push to main. The near-misses are deliberate: a
#: repair PR, a PR whose head branch is called main, branches whose names start
#: with or contain main, a tag, and the non-push events that run ON main.
NOT_A_MAIN_PUSH = {
    "trunk-repair pull_request": REPAIR_PR,
    "plain pull_request": Event("pull_request", "refs/pull/2/merge", ("post-v1",), head_ref="af--x"),
    "unlabeled pull_request": Event("pull_request", "refs/pull/3/merge", (), head_ref="af--x"),
    "pull_request from a head called main": Event("pull_request", "refs/pull/4/merge", (), head_ref="main"),
    "pull_request whose ref is main": Event("pull_request", "refs/heads/main", (), head_ref="af--x"),
    "branch push": Event("push", "refs/heads/af--feature"),
    "integration branch push": Event("push", "refs/heads/integ--post-v1-app"),
    "push to main-prefixed branch": Event("push", "refs/heads/main-backup"),
    "push to nested main branch": Event("push", "refs/heads/release/main"),
    "tag push": Event("push", "refs/tags/main"),
    "schedule on main": Event("schedule", "refs/heads/main"),
    "workflow_dispatch on main": Event("workflow_dispatch", "refs/heads/main"),
    "merge_group on main": Event("merge_group", "refs/heads/main"),
}
ALL_EVENTS = {"push to main": MAIN_PUSH, **NOT_A_MAIN_PUSH}


def _jobs(path: Path = E2E_WORKFLOW) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"]


def _gate() -> str:
    return str(_jobs()["gate"]["runs-on"])


def _with(event: Event, variables: dict[str, str]) -> Event:
    return Event(event.name, event.ref, event.labels, dict(variables), head_ref=event.head_ref)


# --------------------------------------------------------------- structural


def test_gate_carries_the_exact_stacked_route() -> None:
    """if the gate's runs-on drifts from the named route then the evaluated cases below test a different expression"""
    assert _gate() == E2E_GATE_RUNS_ON


def test_trunk_lane_is_one_main_push_disjunct_in_front_of_the_reserve() -> None:
    """if the lane's guard is dropped, widened or moved then main loses its runner or other load gains it"""
    raw = _gate()
    assert raw.startswith("${{ fromJSON(" + f"{E2E_TRUNK_DISJUNCT} || {E2E_MAIN_FIX_DISJUNCT} || "), raw
    assert raw.count(f"vars.{VARIABLE}") == 1, raw
    guard = "(github.event_name == 'push' && github.ref == 'refs/heads/main')"
    assert f"{guard} && vars.{VARIABLE}" == E2E_TRUNK_DISJUNCT
    assert raw.count("'push'") == 1 and raw.count("refs/heads/main") == 1, raw


def test_removing_the_trunk_lane_leaves_the_reserve_route_exactly() -> None:
    """if the gate minus the lane's one disjunct differs from the reserve alone then unset is not a rollback"""
    without_lane = _gate().replace(f"{E2E_MERGE_QUEUE_DISJUNCT} || ", "", 1).replace(f"{E2E_TRUNK_DISJUNCT} || ", "", 1)
    assert without_lane == E2E_REPAIR_RESERVE_RUNS_ON


def test_no_other_job_reads_the_trunk_lane() -> None:
    """if the nightly extended suite or any other job reads CI_RUNS_ON_E2E_TRUNK then it can hold the trunk-only runner for hours"""
    readers = [
        (path.name, job_id)
        for path in sorted(WORKFLOWS.glob("*.yml"))
        for job_id, job in (_jobs(path) or {}).items()
        if VARIABLE in json.dumps(job)
    ]
    assert readers == [("e2e.yml", "gate")], readers
    assert _jobs()["extended"]["runs-on"] == E2E_POOL_RUNS_ON


# ----------------------------------------------------------------- behavior


@pytest.mark.parametrize("name", sorted(ALL_EVENTS))
def test_evaluator_control_reads_the_bare_pool_and_the_reserve_alone(name: str) -> None:
    """if the evaluator misreads the two pre-lane expressions then no routing claim below is measured"""
    event = _with(ALL_EVENTS[name], BOTH_SET)
    assert resolve(E2E_POOL_RUNS_ON, event) == POOL_LABELS
    expected = RESERVE_LABELS if name == "trunk-repair pull_request" else POOL_LABELS
    assert resolve(E2E_REPAIR_RESERVE_RUNS_ON, event) == expected
    assert resolve(E2E_POOL_RUNS_ON, _with(ALL_EVENTS[name], {})) == "ubuntu-latest"


@pytest.mark.parametrize("variables", sorted(TRUNK_SET))
def test_main_push_resolves_to_the_trunk_lane(variables: str) -> None:
    """if a main push does not take CI_RUNS_ON_E2E_TRUNK then main's E2E starves behind the PR backlog"""
    assert resolve(_gate(), _with(MAIN_PUSH, TRUNK_SET[variables])) == TRUNK_LABELS


@pytest.mark.parametrize("variables", sorted(TRUNK_SET))
@pytest.mark.parametrize("name", sorted(NOT_A_MAIN_PUSH))
def test_nothing_but_a_main_push_takes_the_trunk_lane(name: str, variables: str) -> None:
    """if any event but a main push takes CI_RUNS_ON_E2E_TRUNK then that load can occupy the trunk-only runner"""
    event = _with(NOT_A_MAIN_PUSH[name], TRUNK_SET[variables])
    resolved = resolve(_gate(), event)
    assert resolved == resolve(E2E_REPAIR_RESERVE_RUNS_ON, event)
    takes_reserve = name == "trunk-repair pull_request" and variables == "both set"
    assert resolved == (RESERVE_LABELS if takes_reserve else POOL_LABELS)


@pytest.mark.parametrize("variables", sorted(TRUNK_UNSET))
@pytest.mark.parametrize("name", sorted(ALL_EVENTS))
def test_unset_trunk_variable_is_exactly_the_reserve_alone(name: str, variables: str) -> None:
    """if an unset CI_RUNS_ON_E2E_TRUNK changes any event's route then rollback is not a variable unset"""
    event = _with(ALL_EVENTS[name], TRUNK_UNSET[variables])
    resolved = resolve(_gate(), event)
    assert resolved == resolve(E2E_REPAIR_RESERVE_RUNS_ON, event)
    takes_reserve = name == "trunk-repair pull_request" and variables == "reserve only"
    assert resolved == (RESERVE_LABELS if takes_reserve else POOL_LABELS)


@pytest.mark.parametrize("name", sorted(ALL_EVENTS))
def test_no_variables_at_all_is_the_hosted_runner(name: str) -> None:
    """if the hosted fallback is lost then a repo with no runner variables cannot run the gate"""
    assert resolve(_gate(), _with(ALL_EVENTS[name], {})) == "ubuntu-latest"
