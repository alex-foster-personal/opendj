"""The ci.yml `fast` job runs the whole fast tier first, on a pool of its own, and can cancel.

SMARTEST-CI round 6a (specs/ci-fail-fast.md). This pins the job's SHAPE, the part a
reviewer cannot see from a green run: which pool it asks for, that it may cancel the run,
that a thin selection or a thin ledger fails loud, and that the sharded lane is unchanged
except for the ledger guard.

Single-line intent:
  - if the fast job's runs-on drops CI_RUNS_ON_FAST ahead of the Linux pool then legs queue
    behind shards
  - if the fast job loses `actions: write` or `checks: read` then the fail-fast step 403s
  - if the fast job runs on push then a trunk push can cancel its own verdict
  - if the fast job runs on a Trunk queue draft then four ungating legs take pytest slots from PR heads
  - if the fast job's pytest drops --tier-min-selected or --ledger-coverage-min then a thin run
    reads green
  - if the fast job stores durations then four partial legs overwrite the ledger
  - if the shard job loses --ledger-coverage-min then a thin ledger balances shards by count again
  - if the cancel step is not continue-on-error then UNKNOWN reads as a second failure
  - if the affected-test canary is still in ci.yml then two jobs claim the same lane
  - if the legs still run on a PR head whose shards run the whole lane then 17k tests run twice
  - if the scope job's selection is not fail-open then a selector crash silently drops the legs

[if] a pull request opens [then] the fast tier runs every cheap test first, [else stop].
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.requirement("INFRA-03")

CI: Path = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
FAST_RUNS_ON = (
    "${{ fromJSON((github.event_name == 'pull_request' && "
    "contains(github.event.pull_request.labels.*.name, 'ci:trunk-repair')) "
    "&& vars.CI_RUNS_ON_TRUNK || "
    "vars.CI_RUNS_ON_FAST || vars.CI_RUNS_ON_PYTEST || vars.CI_RUNS_ON_E2E || "
    "vars.CI_RUNS_ON_LINUX || '\"ubuntu-latest\"') }}"
)


# The three-part Trunk queue draft test every CI_RUNS_ON_MERGE_QUEUE clause uses.
TRUNK_DRAFT = (
    "startsWith(github.head_ref, 'trunk-merge/') && "
    "github.event.pull_request.user.login == 'trunk-io[bot]' && "
    "github.event.pull_request.head.repo.full_name == github.repository"
)


def _jobs() -> dict:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]


def _pytest_step(job: dict) -> str:
    steps = [s for s in job["steps"] if ".venv/bin/pytest" in (s.get("run") or "")]
    assert len(steps) == 1, f"expected exactly one pytest step, found {len(steps)}"
    return steps[0]["run"]


def _raw_runs_on(job_id: str) -> str:
    lines = CI.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.rstrip() == f"  {job_id}:")
    for line in lines[start + 1 :]:
        if line.startswith("  ") and not line.startswith("    ") and line.strip():
            break
        if line.strip().startswith("runs-on:"):
            return line.strip()[len("runs-on:") :].strip()
    raise AssertionError(f"no runs-on line found for job {job_id!r}")


def test_fast_job_prefers_its_own_pool_then_the_shard_chain() -> None:
    """if the fast job drops the shard's PYTEST/E2E chain then legs land on nucbox and read red

    Run 35107758392 (Wed 16 Sep 2026): the plain LINUX pool put all four legs on
    nucbox and 19 of 20 reds were host-dependent tests that pass on agentbox.
    """
    assert _raw_runs_on("fast") == FAST_RUNS_ON
    shard = _raw_runs_on("test")
    assert shard.endswith(
        "vars.CI_RUNS_ON_PYTEST || vars.CI_RUNS_ON_E2E || vars.CI_RUNS_ON_LINUX || "
        "'\"ubuntu-latest\"') }}"
    ), "the fast chain must stay a suffix of the shard chain; update both together"


def test_fast_job_is_pull_request_only_with_four_legs() -> None:
    """if the fast job runs on push then a trunk push can cancel its own verdict

    A `workflow_dispatch` is the one other admitted event: main-control.yml
    dispatches the tier on main every six hours (tests/quality/
    test_main_control_workflow.py pins the input that keeps the full lane out).
    """
    job = _jobs()["fast"]
    # Gated on the in-run scope decision first (issue #4168): a docs-only pull
    # request must not occupy a pytest runner now that ci.yml triggers on it.
    assert job["needs"] == "scope"
    assert job["if"] == (
        "needs.scope.outputs.in_scope == 'true' && "
        "((github.event_name == 'pull_request' && "
        "needs.scope.outputs.pr_selection != 'full' && !(" + TRUNK_DRAFT + ")) || "
        "(github.event_name == 'workflow_dispatch' && inputs.tier == 'fast'))"
    )
    assert job["strategy"]["matrix"]["leg"] == [1, 2, 3, 4]
    assert job["strategy"]["fail-fast"] is False, "legs must all report; the cancel step decides"


def test_fast_job_is_a_signal_not_a_gate_in_round_6a() -> None:
    """if the fast job blocks before its precision is measured then host noise blocks every PR

    Runs 35107758392 and 35109932422 (Wed 16 Sep 2026): 19 and 8 reds per run
    that main's shards on agentbox do not show, all host-dependent. Round 6b
    flips this to a gate once precision on one host class is measured; that
    flip edits this test on purpose.
    """
    # Pull requests only: the six-hour `tier: fast` control dispatch on main
    # (main-control.yml) has nothing but this job, so there a red leg must
    # fail the run (tests/quality/test_main_control_workflow.py pins that half).
    assert _jobs()["fast"]["continue-on-error"] == "${{ github.event_name == 'pull_request' }}"


def test_fast_job_may_cancel_the_run() -> None:
    """if `actions: write` or `checks: read` is dropped then the fail-fast step dies with 403"""
    job = _jobs()["fast"]
    # checks: read is what lets ci_main_red read main's check-runs for the
    # baseline; without it every leg's fail-fast step crashed on a 403 and the
    # decision was never made (seen live Mon 21 Sep 2026, run 35608165497).
    assert job["permissions"] == {"contents": "read", "actions": "write", "checks": "read"}
    cancel = [s for s in job["steps"] if "scripts.ci_fast_cancel" in (s.get("run") or "")]
    assert len(cancel) == 1
    unknown_note = "UNKNOWN (exit 3) must not read as a second failure"
    assert cancel[0]["continue-on-error"] is True, unknown_note
    assert "steps.fast.outcome == 'failure'" in cancel[0]["if"]
    assert cancel[0]["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert "CI_FAST_CANCEL" in cancel[0]["run"], "disarmed unless the var is 1"
    assert "--dry-run" in cancel[0]["run"], "disarmed unless the var is 1"


def test_artifact_upload_precedes_the_cancel_step() -> None:
    """if the upload runs after the cancel then a cancelled leg loses its log and JUnit"""
    steps = [s.get("name", "") for s in _jobs()["fast"]["steps"]]
    upload = next(i for i, n in enumerate(steps) if n.startswith("Upload the leg"))
    cancel = next(i for i, n in enumerate(steps) if n.startswith("Fail fast"))
    assert upload < cancel


def test_fast_job_pytest_flags_fail_loud_and_never_write_the_ledger() -> None:
    """if --tier-min-selected or --ledger-coverage-min is dropped then a thin run reads green"""
    run = _pytest_step(_jobs()["fast"])
    for flag in (
        "--fast-tier fast",
        "--fast-tier-max-seconds 0.5",
        "--ledger-coverage-min 0.85 --ledger-coverage-warn 0.95",
        "--tier-min-selected 2000",
        "--splits 4 --group ${{ matrix.leg }}",
        "--durations-path .test_durations",
    ):
        assert flag in run, flag
    assert "--store-durations" not in run, "four partial legs must never overwrite the ledger"
    assert "--clean-durations" not in run
    assert "-p scripts." not in run, "the console script cannot import -p plugins; conftest does"


def test_tier_plugins_are_registered_by_the_root_conftest() -> None:
    """if the tier plugins leave conftest then `--fast-tier` is an unknown option in CI"""
    conftest = (CI.parents[2] / "conftest.py").read_text(encoding="utf-8")
    assert '"scripts.pytest_fast_tier"' in conftest
    assert '"scripts.pytest_tier_floor"' in conftest


def test_fast_job_ignores_match_the_shard_job() -> None:
    """if the fast tier ignores a different set than the shards then a leg runs what CI never did"""
    shard = _pytest_step(_jobs()["test"])
    fast = _pytest_step(_jobs()["fast"])
    ignores = lambda run: sorted(tok for tok in run.split() if tok.startswith("--ignore="))  # noqa: E731
    assert ignores(fast) == ignores(shard)
    assert len(ignores(fast)) == 8  # + the playlist-switch bench, hosted by e2e.yml extended


def test_shard_job_gains_only_the_ledger_guard() -> None:
    """if the shard job loses --ledger-coverage-min then a thin ledger balances by count again"""
    shard = _pytest_step(_jobs()["test"])
    assert "--ledger-coverage-min 0.85 --ledger-coverage-warn 0.95" in shard
    assert "-p scripts." not in shard
    assert "--fast-tier" not in shard, "the sharded lane still runs everything (6a is additive)"
    assert "--store-durations --clean-durations" in shard


def test_affected_canary_is_gone() -> None:
    """if the affected-test canary is still in ci.yml then two jobs claim the same lane"""
    assert "affected-canary" not in _jobs()
    assert "affected-canary" not in CI.read_text(encoding="utf-8")


# Re-measured Tue 22 Sep 2026 over every fast-tier leg since 12:00Z Mon 21 Sep
# (863 legs). Job wall on agentbox runners: 231 green legs, median 372 s, p90 468 s,
# max 519 s. On the nucbox-wsl runners, which share one host: 18 green legs, median
# 499 s, p90 662 s, max 708 s, and the 720 s budget killed a leg at 93% with no
# failing test (#3740 leg 3, run 35718455313). Before them, Mon 21 Sep over 117
# agentbox legs: step median 341 s, p90 439 s, max 470 s, and the 480 s budget killed
# #3701 leg 1 at 94%. 1080 s is 1.5x the nucbox-wsl maximum; a budget under it
# re-introduces the budget-kill with nothing to name.
MIN_FAST_WALL_BUDGET_S = 1080
# Provisioning ahead of pytest measured ~2 min warm, more cold, on the same runs.
MIN_FAST_PRE_PYTEST_RESERVE_S = 240


def test_fast_leg_wall_budget_clears_the_measured_pool_maximum() -> None:
    job = _jobs()["fast"]
    run = _pytest_step(job)
    budget = re.search(r"^\s*MDT_FAST_TIMEOUT_S=(\d+)\s*$", run, re.MULTILINE)
    assert budget, (
        f"the fast leg must declare its wall budget as MDT_FAST_TIMEOUT_S=<seconds>:\n{run}"
    )
    budget_s = int(budget.group(1))
    assert budget_s >= MIN_FAST_WALL_BUDGET_S, (
        f"a {budget_s} s leg budget is under the {MIN_FAST_WALL_BUDGET_S} s floor the pool "
        "measurement sets, so it reintroduces the budget-kill at 94% that #3701 leg 1 hit"
    )
    cap_s = job["timeout-minutes"] * 60
    assert cap_s >= budget_s + MIN_FAST_PRE_PYTEST_RESERVE_S, (
        f"the job cap ({cap_s} s) must leave the leg budget ({budget_s} s) plus "
        f"{MIN_FAST_PRE_PYTEST_RESERVE_S} s of provisioning, or the job is CANCELLED by the "
        "cap before the leg's own timeout reports a TIMEOUT by name"
    )


def test_fast_leg_bounds_each_test_under_its_wall_budget() -> None:
    """[if] one test hangs [then] pytest-timeout fails THAT test by name with its stack,
    well before the leg's own wall budget kills the whole leg anonymously (six legs
    stalled at the same 32% mark on Mon 21 Sep 2026 and every kill read only
    "exit 124 after 480s"), [else stop]"""
    run = _pytest_step(_jobs()["fast"])
    # The FLAG on its own line, not the comment above it that quotes the flag:
    # a match inside a comment is not a match in shipped code.
    per_test = re.search(r"^\s*--timeout=(\d+)\s*\\?$", run, re.MULTILINE)
    assert per_test, f"the fast leg carries no per-test --timeout:\n{run}"
    budget = re.search(r"MDT_FAST_TIMEOUT_S=(\d+)", run)
    assert budget, "the fast leg carries no wall budget of its own"
    assert int(per_test.group(1)) * 2 <= int(budget.group(1)), (
        "a per-test ceiling within half the leg budget of the budget itself lets the "
        "leg die first again, and the kill names nothing"
    )
    # The flag is only honored by an installed plugin; an unknown option is a usage
    # error, and a plugin that is merely present in a venv is one `uv sync` from gone.
    pyproject = tomllib.loads((CI.parents[2] / "pyproject.toml").read_text(encoding="utf-8"))
    dev = pyproject["project"]["optional-dependencies"]["dev"]
    assert any(spec.startswith("pytest-timeout") for spec in dev), dev
    # ci.yml provisions the lanes from requirements.txt with `uv pip install --exact`,
    # not from the dev extra: a plugin declared only in pyproject.toml is absent on
    # the runner and `--timeout` becomes a usage error that fails every leg and shard.
    requirements = (CI.parents[2] / "requirements.txt").read_text(encoding="utf-8")
    assert any(line.startswith("pytest-timeout") for line in requirements.splitlines()), (
        "pytest-timeout is missing from requirements.txt, which is what CI installs"
    )


@pytest.mark.requirement("DEVOPS-18")
def test_fast_job_skips_the_same_queue_draft_the_shards_route_to_mq() -> None:
    """[if] the fast skip and the shards' mq clause name different drafts [then] broken, [else stop].

    The skip and the `mq` routing must name one draft. A copy that drifts either
    runs the ungating legs on drafts again or silently drops them from real PR heads.
    """
    assert TRUNK_DRAFT in _raw_runs_on("test"), "the shard job's mq clause no longer matches TRUNK_DRAFT"
    assert f"!({TRUNK_DRAFT})" in _jobs()["fast"]["if"]


def _scope_selection_step() -> dict:
    steps = [s for s in _jobs()["scope"]["steps"] if s.get("id") == "pr-selection"]
    assert len(steps) == 1, "the scope job must decide the PR-head selection exactly once"
    return steps[0]


def test_legs_skip_only_a_pr_head_whose_selection_is_full() -> None:
    """if the legs still run on a FULL PR head then 17k tests run twice; if the skip reaches
    the fast-tier dispatch then main's six-hour control run loses its whole verdict"""
    fast_if = _jobs()["fast"]["if"]
    pr_clause, dispatch_clause = fast_if.split("||")
    assert "needs.scope.outputs.pr_selection != 'full'" in pr_clause
    assert "github.event_name == 'pull_request'" in pr_clause
    assert "pr_selection" not in dispatch_clause
    outputs = _jobs()["scope"]["outputs"]
    assert outputs["pr_selection"] == "${{ steps.pr-selection.outputs.mode }}"


def test_scope_selection_is_fail_open() -> None:
    """if a selector crash or timeout in scope can read as FULL then the legs vanish on
    an unmeasured answer, or a red step blocks the run"""
    step = _scope_selection_step()
    assert step.get("continue-on-error") is True
    assert step.get("if") == "github.event_name == 'pull_request'"
    run = step["run"]
    assert '|| mode=""' in run
    assert 'mode=""' in run.split("*)", 1)[1], "an unexpected answer must clear the mode"
    assert "GITHUB_STEP_SUMMARY=/dev/null" in run, "the shards own the selection summary"


def test_scope_selection_ignores_match_the_shard_job() -> None:
    """if the scope job selects over a different lane than the shards then FULL in scope
    need not mean the shards ran what the legs skip"""
    shard = _pytest_step(_jobs()["test"])
    scope = _scope_selection_step()["run"]
    ignores = lambda run: sorted(tok for tok in run.replace("\\", " ").split() if tok.startswith("--ignore="))  # noqa: E731
    assert ignores(scope) == ignores(shard)
