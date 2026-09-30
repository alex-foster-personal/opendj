"""A trunk fix must never be fully starved behind the general PR-CI backlog.

Mon 14 Sep 2026: with main red, the trunk-repair PR's checks queued behind 60
doomed PR runs with all 30 self-hosted runners busy (see the incident note
referenced by ADR-0041). Two runners (nucbox-wsl, nucbox-wsl-2) are reserved
host-side (nucbox ``~/jobs/runner-governor.sh``, label ``main-fix``, agentbox
and nucbox stripped so the general pool never contends for them) and the
``test`` job's ``runs-on`` prefers ``vars.CI_RUNS_ON_MAIN_FIX`` for exactly
one case: a pull_request carrying the ``ci:trunk-repair`` label. Every other
event, a push to main included, takes the general chain.

Mon 21 Sep 2026 (ADR-NEW-main-pushes-use-the-general-pytest-pool): the
original guard also sent every push to main through the two reserved
runners. Measured that day, main's own shards queued 40-60 minutes behind
the previous merge's while the seven-runner pytest pool served PRs, so trunk
could not prove itself and every PR inherited an unmeasured red (#3708
section 2). The push clause was dropped; the reservation is now spent only
on the repair PR, which is the run the reserve exists to protect.

PR #2654 ("route pytest fast-lane shards off nucbox to agentbox pool")
merged into main the same day, after this branch was cut, prepending its own
``CI_RUNS_ON_PYTEST || CI_RUNS_ON_E2E`` preference in front of
``CI_RUNS_ON_LINUX`` on this exact job. This test's expected string is the
composed chain after merging both changes: the main-fix guard first, then
#2654's full fallback order, unchanged.

This test is a structural pin on the YAML, not a GitHub Actions expression
evaluator (none is available offline): it asserts the exact expression
string, which was hand-verified against 5 real workflow runs on
af--ci-main-fix-runner-reserve (run 34898282105) -- an unset
CI_RUNS_ON_MAIN_FIX and a false condition both fall through to the rest of
the chain, a set variable wins when the condition is true, and the
label-guard clause evaluates to false with no dereference error on a
non-pull_request event (so a push event never touches
github.event.pull_request).

Regression lines:
  - if the ci:trunk-repair guard is dropped then a trunk fix loses its
    reserved pool and starves again exactly as it did on Mon 14 Sep 2026
  - if a main-branch push guard is reintroduced then main's own CI is
    funnelled through two runners again and trunk stops proving itself
  - if the guard does not short-circuit before github.event.pull_request then
    a push event (which has no pull_request context) can error instead of
    silently falling through
  - if the fallback tail (CI_RUNS_ON_PYTEST / CI_RUNS_ON_E2E /
    CI_RUNS_ON_LINUX / ubuntu-latest) is dropped or reordered then an unset
    CI_RUNS_ON_MAIN_FIX (a fork, or before the variable exists) loses #2654's
    nucbox-avoidance routing, not just this ADR's reservation
  - if any OTHER job's runs-on picks up this pattern unintentionally then a
    reviewer cannot tell a deliberate widening from an accidental one
"""

from __future__ import annotations

from pathlib import Path

import yaml

from tests.scripts.ci_runner_routes import LIGHT_RUNS_ON, SHARD_RUNS_ON, SHARD_TAIL

REPO_ROOT = Path(__file__).resolve().parents[2]
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"

SHARD_JOB = "test"

#: The exact expression string this ADR-0041 change installs, composed on
#: top of #2654's CI_RUNS_ON_PYTEST/CI_RUNS_ON_E2E chain. Verified live (run
#: 34898282105): unset var / false cond both fall through to the rest of the
#: chain; a set var wins under a true cond; the label-guard clause alone
#: reads false with no error on a non-pull_request event.
#: Sat 26 Sep 2026 (ADR-NEW-trunk-ci-runs-on-agentbox-hosts-only): a guarded
#: CI_RUNS_ON_TRUNK disjunct now sits between the reserve and #2654's chain. It is
#: pinned, and evaluated, by tests/scripts/test_ci_trunk_agentbox_pool.py.
#: Tue 29 Sep 2026 (ADR-NEW-trunk-queue-drafts-use-a-reserved-runner-pool): a
#: guarded CI_RUNS_ON_MERGE_QUEUE disjunct follows the trunk clause. The composed
#: string lives in tests/scripts/ci_runner_routes.py, evaluated by
#: tests/scripts/test_ci_merge_queue_runner_pool.py.
EXPECTED_RUNS_ON = SHARD_RUNS_ON
MAIN_FIX_DISJUNCT = (
    "(github.event_name == 'pull_request' && "
    "contains(github.event.pull_request.labels.*.name, 'ci:trunk-repair'))"
    " && vars.CI_RUNS_ON_MAIN_FIX"
)

#: Every other CI_RUNS_ON_LINUX-driven job in ci.yml. This set must NOT grow
#: without a deliberate edit to this test -- see the class-not-instance
#: regression line above.
# The `fast` tier job (round 6) is NOT here on purpose: it reads CI_RUNS_ON_FAST
# ahead of the LINUX pool, pinned by tests/scripts/test_ci_fast_tier_job.py.
UNCHANGED_LINUX_JOBS = ["contracts", "frontend-build", "frontend", "quality"]

# Tue 29 Sep 2026: they carry the Trunk-draft merge-queue route in front of the
# light pool, and still never the main-fix reserve.
UNCHANGED_RUNS_ON = LIGHT_RUNS_ON


def _jobs() -> dict:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]


def _raw_runs_on(job_id: str) -> str:
    """Read the job's runs-on as the literal string from the file, not the
    yaml-parsed value -- yaml.safe_load leaves `${{ ... }}` as plain text,
    but going through the raw text keeps this test honest about what a
    workflow-syntax reviewer (or actionlint) actually sees."""
    text = CI.read_text(encoding="utf-8")
    lines = text.splitlines()
    jobs = _jobs()
    job = jobs[job_id]
    # Find the job's runs-on by locating "  <job_id>:" then the next
    # "runs-on:" line before the next top-level-under-jobs key.
    start = next(i for i, line in enumerate(lines) if line.rstrip() == f"  {job_id}:")
    for line in lines[start + 1 :]:
        if line.startswith("  ") and not line.startswith("    ") and line.strip():
            break
        stripped = line.strip()
        if stripped.startswith("runs-on:"):
            return stripped[len("runs-on:") :].strip()
    raise AssertionError(f"no runs-on line found for job {job_id!r}")
    del job  # only used to assert the job exists


def test_main_fix_job_prefers_the_reserved_pool_for_trunk_repair_prs_only() -> None:
    """if the trunk-repair guard is dropped, or a main-push guard returns, the reserve is lost
    or misused"""
    assert SHARD_JOB in _jobs(), f"expected a {SHARD_JOB!r} job in ci.yml"
    raw = _raw_runs_on(SHARD_JOB)
    assert raw == EXPECTED_RUNS_ON, f"test job runs-on changed shape, got: {raw}"
    # The push-to-main clause may guard CI_RUNS_ON_TRUNK (nine agentbox runners),
    # never the two-runner reserve: the reserve disjunct is the first one, whole.
    assert raw.startswith("${{ fromJSON(" + MAIN_FIX_DISJUNCT + " || "), raw
    assert "refs/heads/main" not in MAIN_FIX_DISJUNCT
    assert raw.count("vars.CI_RUNS_ON_MAIN_FIX") == 1, (
        "a push to main must never be routed to the main-fix reserve"
    )


def test_guard_checks_event_name_before_dereferencing_pull_request() -> None:
    """if github.event.pull_request is read before an event_name=='pull_request' check then a push event can error"""
    raw = _raw_runs_on(SHARD_JOB)
    # The pull_request-label clause must be gated by its own event_name check
    # in the same parenthesised group, so a push event's `&&` short-circuits
    # before ever reading github.event.pull_request.
    assert (
        "github.event_name == 'pull_request' && contains(github.event.pull_request" in raw
    ), "label-guard clause must check event_name == 'pull_request' before dereferencing pull_request"


def test_unset_or_non_matching_falls_back_to_the_2654_chain() -> None:
    """if the fallback tail is dropped or reordered then an unset CI_RUNS_ON_MAIN_FIX loses #2654's nucbox-avoidance routing too"""
    raw = _raw_runs_on(SHARD_JOB)
    assert raw.endswith(f"&& vars.CI_RUNS_ON_MERGE_QUEUE || {SHARD_TAIL}"), (
        f"fallback chain must degrade through PYTEST/E2E/LINUX to ubuntu-latest, got: {raw}"
    )


def test_no_other_ci_runs_on_linux_job_changed() -> None:
    """if another job's runs-on picks up this pattern unintentionally then it is an accidental widening"""
    jobs = _jobs()
    seen = [j for j in UNCHANGED_LINUX_JOBS if j in jobs]
    assert seen == UNCHANGED_LINUX_JOBS, f"expected jobs missing from ci.yml: {jobs.keys()}"
    for job_id in seen:
        raw = _raw_runs_on(job_id)
        assert raw == UNCHANGED_RUNS_ON, f"job {job_id!r} runs-on changed unexpectedly: {raw}"
