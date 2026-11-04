"""A trunk fix must never be starved behind the general PR-CI backlog, on any job.

Mon 14 Sep 2026: with main red, the trunk-repair PR's checks queued behind 60
doomed PR runs with all 30 self-hosted runners busy (see the incident note
referenced by ADR-0041). Two runners (nucbox-wsl, nucbox-wsl-2) are reserved
host-side (nucbox ``~/jobs/runner-governor.sh``, label ``main-fix``, agentbox
and nucbox stripped so the general pool never contends for them) and the
``test`` job's ``runs-on`` preferred ``vars.CI_RUNS_ON_MAIN_FIX`` for exactly
one case: a pull_request carrying the ``ci:trunk-repair`` label. Every other
event, a push to main included, takes the general chain.

Mon 21 Sep 2026 (ADR-NEW-main-pushes-use-the-general-pytest-pool): the
original guard also sent every push to main through the two reserved
runners. Measured that day, main's own shards queued 40-60 minutes behind
the previous merge's while the seven-runner pytest pool served PRs, so trunk
could not prove itself and every PR inherited an unmeasured red (#3708
section 2). The push clause was dropped; the reservation is spent only on
the repair PR, which is the run the reserve exists to protect.

PR #2654 ("route pytest fast-lane shards off nucbox to agentbox pool")
prepended its own ``CI_RUNS_ON_PYTEST || CI_RUNS_ON_E2E`` preference in front
of ``CI_RUNS_ON_LINUX`` on the shard job; Sat 26 Sep 2026 a guarded
``CI_RUNS_ON_TRUNK`` disjunct joined (test_ci_trunk_agentbox_pool.py); Tue 29
Sep 2026 the Trunk-draft ``CI_RUNS_ON_MERGE_QUEUE`` route
(test_ci_merge_queue_runner_pool.py); Mon 2 Nov 2026 the main-push
``CI_RUNS_ON_MAIN`` reserve, first of all (test_ci_main_push_runner_reserve.py).

Wed 4 Nov 2026 (ADR-NEW-repair-pr-every-job-on-main-fix-seats): the reserve
widened from the shard job to EVERY ci.yml job. Only the shards preferred the
main-fix seats, so a repair PR's `scope`, `contracts`, `frontend-build`,
`frontend`, `quality` and `ci gate` still queued on the light pool: repair
#8531 sat at "ci scope in_progress" from 06:40Z while agentbox-14/15,
nucbox-wsl and nucbox-wsl-2 (label ``main-fix``) were idle, with 108 runs
queued in the previous 6 h. The disjunct now sits on every job, right after
the main-push reserve where a job has one and first otherwise, and the `fast`
legs prefer it ahead of their CI_RUNS_ON_TRUNK fallback. This test used to pin
the OPPOSITE claim (the four light jobs never carry the reserve); that claim
was the accidental-widening guard of ADR-0041, and the deliberate widening
replaces it with an exact per-job route table.

Non-repair events still resolve through the stacks main already shipped: merge-queue
drafts and the light pool (Tue 29 Sep 2026), the main-push ``CI_RUNS_ON_MAIN`` reserve
(Wed 4 Nov 2026, ``tests/scripts/test_ci_main_push_runner_reserve.py``), and
``quality``'s ``CI_RUNS_ON_QUALITY`` preference ahead of the light pool (Tue 6 Oct 2026,
``tests/scripts/test_ci_quality_runner_pool.py``). Those shapes live in
``REPAIR_ROUTED_JOBS`` via ``ci_runner_routes``; this file only asserts the repair guard
and ``CI_RUNS_ON_MAIN_FIX`` layer on top.

The claims are EVALUATED with the expression evaluator from
test_ci_trunk_agentbox_pool.py, plus structural pins where byte-identity is the
claim (hand-verified live against run 34898282105 for the shard job: an unset
variable or a false guard falls through, a set variable wins under a true
guard, and the label guard reads false with no error on a push event).

Single-line intent:
  - [if] a ci.yml job loses the ci:trunk-repair guard [then] a repair queues behind PRs, [else stop].

Regression lines:
  - if any ci.yml job drops the ci:trunk-repair guard then a trunk fix queues
    behind PR load on that job exactly as #8531 did on Wed 4 Nov 2026
  - if a job carries the guard twice, or reads it ahead of the main-push
    reserve, then precedence between the two reserves is ambiguous
  - if a main-branch push resolves to the main-fix seats then main's own CI is
    funnelled through two runners again and trunk stops proving itself
  - if the guard does not short-circuit before github.event.pull_request then
    a push event (which has no pull_request context) can error instead of
    silently falling through
  - if an ordinary PR, a branch push or a dispatch resolves differently from
    the route minus this disjunct then the widening changed routing it did
    not mean to
  - if an unset CI_RUNS_ON_MAIN_FIX resolves differently from the route minus
    this disjunct for any event then rollback is not a variable delete
  - if the shard fallback tail (CI_RUNS_ON_PYTEST / CI_RUNS_ON_E2E /
    CI_RUNS_ON_LINUX / ubuntu-latest) is dropped or reordered then an unset
    CI_RUNS_ON_MAIN_FIX loses #2654's nucbox-avoidance routing too
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from tests.scripts.ci_runner_routes import (
    MAIN_FIX_DISJUNCT,
    MAIN_RESERVE_DISJUNCT,
    MAIN_SHARD_RUNS_ON,
    REPAIR_ROUTED_JOBS,
    SHARD_TAIL,
    without_main_fix,
)
from tests.scripts.test_ci_trunk_agentbox_pool import (
    LIVE_VARS,
    MAIN_FIX,
    Event,
    resolve,
)
from tests.scripts.vendored_runners import pool_labels

pytestmark = pytest.mark.requirement("DEVOPS-8537")

REPO_ROOT = Path(__file__).resolve().parents[2]
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"

SHARD_JOB = "test"
EXPECTED_RUNS_ON = MAIN_SHARD_RUNS_ON

MAIN_VARS = {**LIVE_VARS, "CI_RUNS_ON_MAIN": '["self-hosted","linux","main-only"]'}


def _events(variables: dict[str, str]) -> dict[str, Event]:
    return {
        "repair-pr": Event("pull_request", "refs/pull/1/merge", ("ci:trunk-repair",), variables),
        "plain-pr": Event("pull_request", "refs/pull/2/merge", ("area:ci",), variables),
        "main-push": Event("push", "refs/heads/main", variables=variables),
        "branch-push": Event("push", "refs/heads/af--feature", variables=variables),
        "main-dispatch": Event("workflow_dispatch", "refs/heads/main", variables=variables),
        "main-schedule": Event("schedule", "refs/heads/main", variables=variables),
    }


NOT_A_REPAIR_PR = ["plain-pr", "main-push", "branch-push", "main-dispatch", "main-schedule"]


def _jobs() -> dict:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]


def _raw_runs_on(job_id: str) -> str:
    """Read the job's runs-on as the literal string from the file, not the
    yaml-parsed value -- yaml.safe_load leaves `${{ ... }}` as plain text,
    but going through the raw text keeps this test honest about what a
    workflow-syntax reviewer (or actionlint) actually sees."""
    lines = CI.read_text(encoding="utf-8").splitlines()
    assert job_id in _jobs(), f"expected a {job_id!r} job in ci.yml"
    start = next(i for i, line in enumerate(lines) if line.rstrip() == f"  {job_id}:")
    for line in lines[start + 1 :]:
        if line.startswith("  ") and not line.startswith("    ") and line.strip():
            break
        stripped = line.strip()
        if stripped.startswith("runs-on:"):
            return stripped[len("runs-on:") :].strip()
    raise AssertionError(f"no runs-on line found for job {job_id!r}")


def _outcome(runs_on: str, event: Event) -> object:
    """The resolved runner, or the error type: the token jobs have no hosted fallback,
    so with no variables at all they fail loudly, and must keep failing the same way."""
    try:
        return resolve(runs_on, event)
    except json.JSONDecodeError as error:
        return type(error)


# --------------------------------------------------------------- structural


def test_the_route_table_names_every_ci_job_exactly_once() -> None:
    """[if] a ci.yml job is missing from REPAIR_ROUTED_JOBS [then] its route is unpinned, [else stop]."""
    assert set(_jobs()) == set(REPAIR_ROUTED_JOBS), set(_jobs()) ^ set(REPAIR_ROUTED_JOBS)


@pytest.mark.parametrize("job_id", sorted(REPAIR_ROUTED_JOBS))
def test_every_job_carries_the_repair_reserve_once_after_the_main_push_reserve(job_id: str) -> None:
    """[if] a job drops, doubles or misplaces the repair guard [then] fail, [else stop]."""
    raw = _raw_runs_on(job_id)
    assert raw == REPAIR_ROUTED_JOBS[job_id], f"job {job_id!r} runs-on changed shape, got: {raw}"
    assert raw.count("vars.CI_RUNS_ON_MAIN_FIX") == 1, raw
    assert raw.count(MAIN_FIX_DISJUNCT) == 1, raw
    if MAIN_RESERVE_DISJUNCT in raw:
        assert raw.startswith(f"${{{{ fromJSON({MAIN_RESERVE_DISJUNCT} || {MAIN_FIX_DISJUNCT} || "), raw
    else:
        assert raw.startswith(f"${{{{ fromJSON({MAIN_FIX_DISJUNCT} || "), raw
    assert "refs/heads/main" not in MAIN_FIX_DISJUNCT


def test_main_fix_job_prefers_the_reserved_pool_for_trunk_repair_prs_only() -> None:
    """[if] the shard route drifts or a main-push guard returns [then] fail, [else stop]."""
    raw = _raw_runs_on(SHARD_JOB)
    assert raw == EXPECTED_RUNS_ON, f"test job runs-on changed shape, got: {raw}"
    # The push-to-main clause may guard CI_RUNS_ON_TRUNK (nine agentbox runners),
    # never the two-runner reserve: the reserve disjunct is whole and read once.
    assert raw.count("vars.CI_RUNS_ON_MAIN_FIX") == 1, (
        "a push to main must never be routed to the main-fix reserve"
    )


@pytest.mark.parametrize("job_id", sorted(REPAIR_ROUTED_JOBS))
def test_guard_checks_event_name_before_dereferencing_pull_request(job_id: str) -> None:
    """[if] pull_request is dereferenced before the event_name check [then] a push errors, [else stop]."""
    raw = _raw_runs_on(job_id)
    # The pull_request-label clause must be gated by its own event_name check
    # in the same parenthesised group, so a push event's `&&` short-circuits
    # before ever reading github.event.pull_request.
    assert (
        "github.event_name == 'pull_request' && contains(github.event.pull_request" in raw
    ), "label-guard clause must check event_name == 'pull_request' before dereferencing pull_request"


def test_unset_or_non_matching_falls_back_to_the_2654_chain() -> None:
    """[if] the shard fallback tail is dropped or reordered [then] #2654 routing is lost, [else stop]."""
    raw = _raw_runs_on(SHARD_JOB)
    assert raw.endswith(f"&& vars.CI_RUNS_ON_MERGE_QUEUE || {SHARD_TAIL}"), (
        f"fallback chain must degrade through PYTEST/E2E/LINUX to ubuntu-latest, got: {raw}"
    )


# ---------------------------------------------------------------- evaluated


def test_evaluator_control_sees_the_guard_fire_on_a_repair_pr_only() -> None:
    """[if] the evaluator cannot tell a repair PR from the rest [then] nothing is measured, [else stop]."""
    guard = "${{ " + MAIN_FIX_DISJUNCT.split(" && vars.")[0] + " }}"
    events = _events(LIVE_VARS)
    assert resolve(guard, events["repair-pr"]) is True
    for name in NOT_A_REPAIR_PR:
        assert resolve(guard, events[name]) is not True, name


@pytest.mark.parametrize("job_id", sorted(REPAIR_ROUTED_JOBS))
def test_a_repair_pr_resolves_every_job_to_the_main_fix_seats(job_id: str) -> None:
    """[if] a repair PR resolves any job off CI_RUNS_ON_MAIN_FIX [then] it queues behind PRs, [else stop]."""
    assert resolve(_raw_runs_on(job_id), _events(MAIN_VARS)["repair-pr"]) == pool_labels("main-fix")


@pytest.mark.parametrize("event_name", NOT_A_REPAIR_PR)
@pytest.mark.parametrize("job_id", sorted(REPAIR_ROUTED_JOBS))
def test_every_other_event_resolves_exactly_as_without_the_reserve(job_id: str, event_name: str) -> None:
    """[if] any non-repair event resolves unlike the route minus the guard [then] fail, [else stop]."""
    runs_on = _raw_runs_on(job_id)
    event = _events(MAIN_VARS)[event_name]
    assert _outcome(runs_on, event) == _outcome(without_main_fix(runs_on), event)
    assert _outcome(runs_on, event) != pool_labels("main-fix")


def test_main_fix_label_matches_vendored_v1_pool() -> None:
    """Routing labels are in v1; v1 seats are agentbox-14/15 only (nucbox-wsl pair is outside v1)."""
    # nucbox-wsl and nucbox-wsl-2 remain main-fix seats in this module's docstring and keep
    # their current (non-v1) source until a v2 inventory covers them.
    assert json.loads(MAIN_FIX) == pool_labels("main-fix")


@pytest.mark.parametrize("variables", [LIVE_VARS, MAIN_VARS, {}], ids=["live", "main", "bare"])
@pytest.mark.parametrize("event_name", ["repair-pr", *NOT_A_REPAIR_PR])
@pytest.mark.parametrize("job_id", sorted(REPAIR_ROUTED_JOBS))
def test_unset_main_fix_variable_is_exactly_the_route_without_it(
    job_id: str, event_name: str, variables: dict[str, str]
) -> None:
    """[if] an unset CI_RUNS_ON_MAIN_FIX changes any route [then] rollback is not a delete, [else stop]."""
    runs_on = _raw_runs_on(job_id)
    before = without_main_fix(runs_on)
    assert before != runs_on
    unset = {k: v for k, v in variables.items() if k != "CI_RUNS_ON_MAIN_FIX"}
    event = _events(unset)[event_name]
    assert _outcome(runs_on, event) == _outcome(before, event)
