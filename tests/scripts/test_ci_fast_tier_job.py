"""The ci.yml `fast` job runs the whole fast tier first, on a pool of its own, and can cancel.

SMARTEST-CI round 6a (specs/ci-fail-fast.md). This pins the job's SHAPE, the part a
reviewer cannot see from a green run: which pool it asks for, that it may cancel the run,
that a thin selection or a thin ledger fails loud, and that the sharded lane is unchanged
except for the ledger guard.

Single-line intent:
  - if the fast job's runs-on drops CI_RUNS_ON_FAST ahead of the Linux pool then legs queue
    behind shards
  - if the fast job loses `actions: write` then the cancel step dies with 403 on a genuine red
  - if the fast job runs on push then a trunk push can cancel its own verdict
  - if the fast job's pytest drops --tier-min-selected or --ledger-coverage-min then a thin run
    reads green
  - if the fast job stores durations then four partial legs overwrite the ledger
  - if the shard job loses --ledger-coverage-min then a thin ledger balances shards by count again
  - if the cancel step is not continue-on-error then UNKNOWN reads as a second failure
  - if the affected-test canary is still in ci.yml then two jobs claim the same lane

[if] a pull request opens [then] the fast tier runs every cheap test first, [else stop].
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.requirement("INFRA-03")

CI: Path = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
FAST_RUNS_ON = (
    "${{ fromJSON(vars.CI_RUNS_ON_FAST || vars.CI_RUNS_ON_LINUX || '\"ubuntu-latest\"') }}"
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


def test_fast_job_prefers_its_own_pool_then_linux_then_hosted() -> None:
    """if CI_RUNS_ON_FAST is not first then a 4-minute leg queues behind a 20-minute shard"""
    assert _raw_runs_on("fast") == FAST_RUNS_ON


def test_fast_job_is_pull_request_only_with_four_legs() -> None:
    """if the fast job runs on push then a trunk push can cancel its own verdict"""
    job = _jobs()["fast"]
    assert job["if"] == "github.event_name == 'pull_request'"
    assert job["strategy"]["matrix"]["leg"] == [1, 2, 3, 4]
    assert job["strategy"]["fail-fast"] is False, "legs must all report; the cancel step decides"


def test_fast_job_may_cancel_the_run() -> None:
    """if `actions: write` is dropped then `gh run cancel` dies with 403 on a genuine red"""
    job = _jobs()["fast"]
    assert job["permissions"] == {"contents": "read", "actions": "write"}
    cancel = [s for s in job["steps"] if "scripts.ci_fast_cancel" in (s.get("run") or "")]
    assert len(cancel) == 1
    unknown_note = "UNKNOWN (exit 3) must not read as a second failure"
    assert cancel[0]["continue-on-error"] is True, unknown_note
    assert "steps.fast.outcome == 'failure'" in cancel[0]["if"]
    assert cancel[0]["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert "CI_FAST_CANCEL" in cancel[0]["run"], "disarmed unless the var is 1"
    assert "--dry-run" in cancel[0]["run"], "disarmed unless the var is 1"


def test_fast_job_pytest_flags_fail_loud_and_never_write_the_ledger() -> None:
    """if --tier-min-selected or --ledger-coverage-min is dropped then a thin run reads green"""
    run = _pytest_step(_jobs()["fast"])
    for flag in (
        "-p scripts.pytest_fast_tier",
        "-p scripts.pytest_tier_floor",
        "--fast-tier fast",
        "--fast-tier-max-seconds 0.5",
        "--ledger-coverage-min 0.95",
        "--tier-min-selected 2000",
        "--splits 4 --group ${{ matrix.leg }}",
        "--durations-path .test_durations",
    ):
        assert flag in run, flag
    assert "--store-durations" not in run, "four partial legs must never overwrite the ledger"
    assert "--clean-durations" not in run


def test_fast_job_ignores_match_the_shard_job() -> None:
    """if the fast tier ignores a different set than the shards then a leg runs what CI never did"""
    shard = _pytest_step(_jobs()["test"])
    fast = _pytest_step(_jobs()["fast"])
    ignores = lambda run: sorted(tok for tok in run.split() if tok.startswith("--ignore="))  # noqa: E731
    assert ignores(fast) == ignores(shard)
    assert len(ignores(fast)) == 7


def test_shard_job_gains_only_the_ledger_guard() -> None:
    """if the shard job loses --ledger-coverage-min then a thin ledger balances by count again"""
    shard = _pytest_step(_jobs()["test"])
    assert "-p scripts.pytest_fast_tier --ledger-coverage-min 0.95" in shard
    assert "--fast-tier" not in shard, "the sharded lane still runs everything (6a is additive)"
    assert "--store-durations --clean-durations" in shard


def test_affected_canary_is_gone() -> None:
    """if the affected-test canary is still in ci.yml then two jobs claim the same lane"""
    assert "affected-canary" not in _jobs()
    assert "affected-canary" not in CI.read_text(encoding="utf-8")
