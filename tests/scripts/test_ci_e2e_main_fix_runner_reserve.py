"""A trunk-repair PR's e2e gate must never be starved behind the e2e backlog.

Wed 7 and Thu 8 Oct 2026 (ICE row INC-5): main stopped merging twice for 6-7 h
because each `ci:trunk-repair` PR's `e2e gate` job queued behind the general
backlog on the 4 e2e-labeled runners. ci.yml's pytest `test` job already
reserves runners for exactly this case (ADR-0041, pinned by
tests/scripts/test_ci_main_fix_runner_reserve.py); the e2e gate had no
equivalent, so a repair PR's pytest went green fast and its e2e still waited.

ADR-NEW-e2e-trunk-repair-runner-reserve mirrors that reserve onto e2e.yml's
`gate` job: a pull_request carrying the `ci:trunk-repair` label prefers the
NEW repo variable `vars.CI_RUNS_ON_E2E_MAIN_FIX`, and everything else falls
through unchanged to `vars.CI_RUNS_ON_E2E || '"ubuntu-latest"'`. An unset
variable is today's routing exactly.

Stacked route (ADR-NEW-main-e2e-gets-a-trunk-runner-lane, E2ELANE-01): the gate
now carries one more disjunct in FRONT of the reserve, a push to main preferring
`vars.CI_RUNS_ON_E2E_TRUNK`. Three pins here were written for the reserve being
the only disjunct and are restated for the stacked shape with their intent kept:
the reserve disjunct appears once, byte-identical, immediately before the pool
fallback; the reserve variable is reachable only by a `ci:trunk-repair`
pull_request (evaluated, with the trunk lane set and unset); and removing the
reserve leaves the trunk lane plus the pool, removing both leaves the pool.

This is a structural pin on the YAML plus behavior claims evaluated by the
offline expression evaluator in tests/scripts/test_ci_trunk_agentbox_pool.py
(the same one the merge-queue and trunk-pool routes are measured with). It is
not GitHub's evaluator; the guard clause is byte-identical to the one on the
`test` job, which ADR-0041 hand-verified against real workflow runs.

Regression lines:
  - if the ci:trunk-repair guard is dropped then every e2e gate run takes the
    reserve and the reserve is just a second backlog
  - if the guard is widened to a push (or any non-pull_request event) then
    main's own e2e and every branch push compete for the reserve
  - if the event_name check does not come first then a push, schedule or
    dispatch run dereferences github.event.pull_request
  - if the fallback tail is dropped or reordered then an unset
    CI_RUNS_ON_E2E_MAIN_FIX stops being today's routing
  - if any other job (the nightly `extended` job included) picks up the
    variable then a reviewer cannot tell a deliberate widening from an
    accidental one
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
    E2E_POOL_TAIL,
    E2E_TRUNK_DISJUNCT,
    SHARD_RUNS_ON,
)
from tests.scripts.test_ci_trunk_agentbox_pool import LIVE_VARS, Event, resolve

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
E2E = WORKFLOWS / "e2e.yml"

GATE_JOB = "gate"
VARIABLE = "CI_RUNS_ON_E2E_MAIN_FIX"
#: The value the host owner sets once a runner carries the label (see the ADR).
E2E_MAIN_FIX = '["self-hosted","linux","e2e-main-fix"]'
VARS = {**LIVE_VARS, VARIABLE: E2E_MAIN_FIX}
E2E_POOL = json.loads(LIVE_VARS["CI_RUNS_ON_E2E"])
#: The trunk lane stacked in front of the reserve, and the value it is set to.
TRUNK_VARIABLE = "CI_RUNS_ON_E2E_TRUNK"
E2E_TRUNK = '["self-hosted","linux","e2e-trunk"]'

REPAIR_PR = Event("pull_request", "refs/pull/1/merge", ("ci:trunk-repair",), VARS)
PLAIN_PR = Event("pull_request", "refs/pull/2/merge", ("area:ci",), VARS)
NOT_A_REPAIR_PR = {
    "plain pull_request": PLAIN_PR,
    "unlabeled pull_request": Event("pull_request", "refs/pull/3/merge", (), VARS),
    "push to main": Event("push", "refs/heads/main", variables=VARS),
    "branch push": Event("push", "refs/heads/af--feature", variables=VARS),
    "schedule": Event("schedule", "refs/heads/main", variables=VARS),
    "workflow_dispatch": Event("workflow_dispatch", "refs/heads/main", variables=VARS),
}


def _jobs(path: Path = E2E) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"]


def _runs_on(job_id: str) -> str:
    return str(_jobs()[job_id]["runs-on"])


# --------------------------------------------------------------- structural


def test_gate_prefers_the_e2e_reserve_for_trunk_repair_prs_only() -> None:
    """if the trunk-repair guard is dropped or widened then the reserve is lost or misused"""
    raw = _runs_on(GATE_JOB)
    assert raw == E2E_GATE_RUNS_ON, f"e2e gate runs-on changed shape, got: {raw}"
    assert raw.count(E2E_MAIN_FIX_DISJUNCT) == 1, raw
    assert raw.replace(f"{E2E_MERGE_QUEUE_DISJUNCT} || ", "", 1).endswith(f" || {E2E_MAIN_FIX_DISJUNCT} || {E2E_POOL_TAIL}"), raw
    assert raw.count(f"vars.{VARIABLE}") == 1, raw
    guard = E2E_MAIN_FIX_DISJUNCT
    assert "refs/heads/main" not in guard and "'push'" not in guard, guard


def test_guard_checks_event_name_before_dereferencing_pull_request() -> None:
    """if github.event.pull_request is read before the event_name check then a push can error"""
    raw = _runs_on(GATE_JOB)
    guard = "(github.event_name == 'pull_request' && contains(github.event.pull_request"
    assert guard in raw
    assert raw.index("github.event_name == 'pull_request'") < raw.index("github.event.pull_request")


def test_guard_is_the_same_spelling_as_the_pytest_reserve() -> None:
    """if the e2e guard drifts from ADR-0041's then the two reserves disagree on a repair PR"""
    pytest_guard = E2E_MAIN_FIX_DISJUNCT.removesuffix(f" && vars.{VARIABLE}")
    assert SHARD_RUNS_ON.startswith(f"${{{{ fromJSON({pytest_guard} && vars.CI_RUNS_ON_MAIN_FIX || ")


def test_removing_the_reserve_restores_todays_expression() -> None:
    """if the gate minus the one disjunct differs from before then unset is not today"""
    raw = _runs_on(GATE_JOB).replace(f"{E2E_MERGE_QUEUE_DISJUNCT} || ", "", 1)
    without_reserve = raw.replace(f"{E2E_MAIN_FIX_DISJUNCT} || ", "", 1)
    assert without_reserve == "${{ fromJSON(" + f"{E2E_TRUNK_DISJUNCT} || {E2E_POOL_TAIL}"
    assert without_reserve.replace(f"{E2E_TRUNK_DISJUNCT} || ", "", 1) == E2E_POOL_RUNS_ON


def test_no_other_job_reads_the_e2e_reserve() -> None:
    """if another job picks up the variable then it is an accidental widening"""
    readers = [
        (path.name, job_id)
        for path in sorted(WORKFLOWS.glob("*.yml"))
        for job_id, job in (_jobs(path) or {}).items()
        if VARIABLE in json.dumps(job)
    ]
    assert readers == [("e2e.yml", GATE_JOB)], readers
    assert _runs_on("extended") == E2E_POOL_RUNS_ON, "the nightly job keeps the e2e pool"


# ----------------------------------------------------------------- behavior


def test_evaluator_control_reads_the_pre_change_gate_as_the_e2e_pool() -> None:
    """if the evaluator misreads today's gate expression then no routing claim is measured"""
    assert resolve(E2E_POOL_RUNS_ON, REPAIR_PR) == E2E_POOL
    unset = Event("pull_request", "refs/pull/1/merge", ("ci:trunk-repair",), {})
    assert resolve(E2E_POOL_RUNS_ON, unset) == "ubuntu-latest"


def test_trunk_repair_pr_resolves_to_the_e2e_reserve() -> None:
    """if a repair PR does not resolve to the reserve then it queues behind the backlog"""
    assert resolve(_runs_on(GATE_JOB), REPAIR_PR) == json.loads(E2E_MAIN_FIX)


@pytest.mark.parametrize("trunk_lane", ["trunk lane unset", "trunk lane set"])
@pytest.mark.parametrize("name", sorted(NOT_A_REPAIR_PR))
def test_only_a_trunk_repair_pr_can_select_the_e2e_reserve(name: str, trunk_lane: str) -> None:
    """if any event but a ci:trunk-repair pull_request selects CI_RUNS_ON_E2E_MAIN_FIX then the reserve is misused"""
    lane_set = trunk_lane == "trunk lane set"
    variables = {**VARS, TRUNK_VARIABLE: E2E_TRUNK} if lane_set else dict(VARS)
    event = NOT_A_REPAIR_PR[name]
    probe = Event(event.name, event.ref, event.labels, variables)
    takes_trunk_lane = lane_set and name == "push to main"
    expected = json.loads(E2E_TRUNK) if takes_trunk_lane else E2E_POOL
    assert resolve(_runs_on(GATE_JOB), probe) == expected
    repair = Event(REPAIR_PR.name, REPAIR_PR.ref, REPAIR_PR.labels, variables)
    assert resolve(_runs_on(GATE_JOB), repair) == json.loads(E2E_MAIN_FIX)


@pytest.mark.parametrize("name", sorted(NOT_A_REPAIR_PR))
def test_every_other_event_keeps_todays_route(name: str) -> None:
    """if any other event takes the reserve then the reserve becomes a second backlog"""
    event = NOT_A_REPAIR_PR[name]
    assert resolve(_runs_on(GATE_JOB), event) == resolve(E2E_POOL_RUNS_ON, event) == E2E_POOL


@pytest.mark.parametrize("name", ["trunk-repair pull_request", *sorted(NOT_A_REPAIR_PR)])
def test_unset_reserve_variable_is_exactly_todays_routing(name: str) -> None:
    """if an unset CI_RUNS_ON_E2E_MAIN_FIX changes any route then the change is not inert"""
    event = REPAIR_PR if name == "trunk-repair pull_request" else NOT_A_REPAIR_PR[name]
    unset = Event(event.name, event.ref, event.labels, dict(LIVE_VARS))
    assert resolve(_runs_on(GATE_JOB), unset) == resolve(E2E_POOL_RUNS_ON, unset) == E2E_POOL
    bare = Event(event.name, event.ref, event.labels, {})
    assert resolve(_runs_on(GATE_JOB), bare) == "ubuntu-latest"
