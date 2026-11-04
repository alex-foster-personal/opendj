"""A push to main prefers a reserved runner pool (CI_RUNS_ON_MAIN), Mon 2 Nov 2026.

The throughput session measured 584 runs: main's verdict took a median 34 min,
p90 62, max 151, and most of it was runner queue behind PR load. The ci.yml jobs
on main's verdict path (scope, test, ci-gate; the Trunk quarantine pair until Wed 4
Nov 2026) and ci-insights, which runs after ci-gate while the main-push concurrency
group holds the next main run pending until it completes, each carry,
first in its `runs-on`, the guarded disjunct
`(push && refs/heads/main) && vars.CI_RUNS_ON_MAIN`. The variable is opt-in: unset
it is the empty string, which is falsy, so `'' || X` is X and every event resolves
exactly as before. Setting it (a label set on reserved runners) is a repo-variable
change made after merge, not part of this test.

Wed 4 Nov 2026: contracts, frontend-build, frontend and quality carry it too. They are
not on `ci gate`'s path, but they hold the RUN's conclusion, which the checkpoint verdict
reads: on run 41769345830 (repair merge f2b7a4b88b, 03:48Z) quality sat queued 26 min on
the light pool behind PR load while five main-only and three main-fix runners were idle.

Regression lines:
  - if a main push does not resolve to CI_RUNS_ON_MAIN on any reserved job when it
    is set then main's verdict keeps queueing behind PR load
  - if contracts, frontend-build, frontend or quality leaves the reserve then a main
    run's conclusion waits on the light pool's queue depth again
  - if ci-insights does not take CI_RUNS_ON_MAIN on a main push then every main cycle
    waits for a light-pool seat after ci gate before the next main run can start
  - if an unset CI_RUNS_ON_MAIN resolves differently from the route without the
    disjunct for any event then the change is not a no-op until the variable is set
  - if a PR, a non-main push or a dispatch on main resolves to CI_RUNS_ON_MAIN then
    the reserve is shared with PR load and stops being a reserve
  - if any other ci.yml job reads CI_RUNS_ON_MAIN then the reserve widens by accident
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from tests.scripts.ci_runner_routes import (
    MAIN_RESERVE_DISJUNCT,
    MAIN_RESERVED_JOBS,
    TOKEN_RUNS_ON,
    with_main_fix,
    with_main_reserve,
    without_main_reserve,
)
from tests.scripts.test_ci_trunk_agentbox_pool import LIVE_VARS, Event, resolve

REPO_ROOT = Path(__file__).resolve().parents[2]
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"

MAIN_POOL = '["self-hosted","linux","main"]'
MAIN_VARS = {**LIVE_VARS, "CI_RUNS_ON_MAIN": MAIN_POOL}
MERGE_QUEUE_VARS = {**LIVE_VARS, "CI_RUNS_ON_MERGE_QUEUE": '["self-hosted","linux","mq"]'}


def _events(variables: dict[str, str]) -> dict[str, Event]:
    return {
        "main-push": Event("push", "refs/heads/main", variables=variables),
        "branch-push": Event("push", "refs/heads/af--feature", variables=variables),
        "main-dispatch": Event("workflow_dispatch", "refs/heads/main", variables=variables),
        "plain-pr": Event("pull_request", "refs/pull/2/merge", ("area:ci",), variables),
        "repair-pr": Event("pull_request", "refs/pull/1/merge", ("ci:trunk-repair",), variables),
        "trunk-draft": Event(
            "pull_request",
            "refs/pull/3/merge",
            variables=variables,
            head_ref="trunk-merge/main/1",
            author="trunk-io[bot]",
        ),
    }


NOT_MAIN_PUSH = ["branch-push", "main-dispatch", "plain-pr", "repair-pr", "trunk-draft"]


def _jobs() -> dict:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]


def _runs_on(job_id: str) -> str:
    return str(_jobs()[job_id]["runs-on"])


def _outcome(runs_on: str, event: Event) -> object:
    """The resolved runner, or the error type: the token route has no hosted fallback,
    so with no variables at all it fails loudly, and must keep failing the same way."""
    try:
        return resolve(runs_on, event)
    except json.JSONDecodeError as error:
        return type(error)


# --------------------------------------------------------------- structural


@pytest.mark.parametrize("job_id", sorted(MAIN_RESERVED_JOBS))
def test_reserved_job_carries_the_main_disjunct_first(job_id: str) -> None:
    """if the disjunct is not first then a set main reserve loses to an earlier route"""
    runs_on = _runs_on(job_id)
    assert runs_on == MAIN_RESERVED_JOBS[job_id]
    assert runs_on.startswith("${{ fromJSON(" + MAIN_RESERVE_DISJUNCT + " || "), runs_on
    assert runs_on.count("vars.CI_RUNS_ON_MAIN ") == 1, runs_on


def test_no_other_job_reads_the_main_reserve() -> None:
    """if any other ci.yml job reads CI_RUNS_ON_MAIN then the reserve widens by accident"""
    readers = {
        job_id
        for job_id, job in _jobs().items()
        if "vars.CI_RUNS_ON_MAIN " in str(job.get("runs-on", ""))
        or "vars.CI_RUNS_ON_MAIN)" in str(job.get("runs-on", ""))
    }
    assert readers == set(MAIN_RESERVED_JOBS)


# ---------------------------------------------------------------- evaluated


@pytest.mark.parametrize("job_id", sorted(MAIN_RESERVED_JOBS))
def test_main_push_takes_the_reserve_when_set(job_id: str) -> None:
    """if a main push does not resolve to CI_RUNS_ON_MAIN then main keeps queueing behind PRs"""
    assert resolve(_runs_on(job_id), _events(MAIN_VARS)["main-push"]) == json.loads(MAIN_POOL)


@pytest.mark.parametrize("event_name", NOT_MAIN_PUSH)
@pytest.mark.parametrize("job_id", sorted(MAIN_RESERVED_JOBS))
def test_other_events_never_take_the_reserve(job_id: str, event_name: str) -> None:
    """if anything but a main push takes CI_RUNS_ON_MAIN then PR load shares the reserve"""
    variables = MAIN_VARS | MERGE_QUEUE_VARS
    runs_on = _runs_on(job_id)
    event = _events(variables)[event_name]
    assert resolve(runs_on, event) == resolve(without_main_reserve(runs_on), event)
    assert resolve(runs_on, event) != json.loads(MAIN_POOL)


@pytest.mark.parametrize("variables", [LIVE_VARS, MERGE_QUEUE_VARS, {}], ids=["live", "mq", "bare"])
@pytest.mark.parametrize("event_name", ["main-push", *NOT_MAIN_PUSH])
@pytest.mark.parametrize("job_id", sorted(MAIN_RESERVED_JOBS))
def test_unset_main_reserve_is_exactly_todays_routing(
    job_id: str, event_name: str, variables: dict[str, str]
) -> None:
    """if an unset CI_RUNS_ON_MAIN changes any route then merging this is not a no-op"""
    runs_on = _runs_on(job_id)
    before = without_main_reserve(runs_on)
    assert before != runs_on
    event = _events(variables)[event_name]
    assert _outcome(runs_on, event) == _outcome(before, event)


def test_ci_insights_is_reserved() -> None:
    """if ci-insights leaves the reserve then the next main run waits on its light-pool seat"""
    # Wed 4 Nov 2026: the ci:trunk-repair reserve follows the main-push one on every job.
    assert MAIN_RESERVED_JOBS["ci-insights"] == with_main_fix(with_main_reserve(TOKEN_RUNS_ON))
