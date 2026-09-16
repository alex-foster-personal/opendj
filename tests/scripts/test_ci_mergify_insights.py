"""The pytest fast lane records per-test health in Mergify CI Insights.

Round 1 is MEASURE ONLY (DevOps ruling, Wed 16 Sep 2026): the shard's verdict
is still `exit "$rc"` from pytest itself, and the CI Insights steps only
record. Quarantine, which can subtract a known-flaky failure from an exit
code, is deliberately NOT armed until there is a baseline to arm it against.

The value this lane buys is one defect this repo has a measured instance of:
a SILENT FAILURE, where the runner exits non-zero while the JUnit report
carries no failures (a collection error, an OOM, a crashed worker), so a gate
certifies something it never measured. scripts/affected_tests.py returned 0
modules with rc=0 on a bad path and its canary read green for 25 of 28 runs
without executing a single test. `test_step_outcome` is what detects that
shape structurally instead of by someone noticing.

Regression lines:
  - if the pytest step drops --junitxml then no report is written, the
    upload has nothing to send, and the lane is silently unmeasured
  - if --junitxml and report_path stop naming the same file then the upload
    finds nothing while both halves still look correctly configured
  - if either filename stops varying per shard then five shards race on one
    path and whichever finishes last is the only one recorded
  - if job_name is dropped then the action defaults to the GitHub job name,
    which is identical across all five shards, and every shard's results are
    filed under one name so the per-test history is worthless
  - if job_name stops varying per shard it fails the same way, silently
  - if test_step_outcome is dropped then MERGIFY_TEST_EXIT_CODE is never set
    and silent-failure detection -- the reason this lane was wired first --
    never fires, while uploads keep succeeding so nothing looks wrong
  - if the pytest step's id and the outcome expression stop agreeing then
    test_step_outcome resolves to an empty string, which the action treats
    the same as omitting it, so detection is off with no error anywhere
  - if the CI Insights step loses continue-on-error then a Mergify outage,
    rate limit or expired token paints every pull request red on a lane that
    is only supposed to be measuring
  - if the upload-verdict guard is dropped then a 'rejected' upload records
    NO test data while the step still reads as having run, and the flake
    baseline is built from missing runs
  - if the guard stops distinguishing 'rejected' from 'failed' from empty
    then a permanent scope problem reads like a transient blip
  - if the action stops being pinned to a full commit SHA then a third party
    can change what runs inside this repo's CI without review
  - if MERGIFY_TOKEN leaves the job env then the `if:` guards cannot fire at
    all (a secret is not readable from a step condition), so an unprovisioned
    token and a broken one render identically
  - if the steps stop gating on the token then every shard of every pull
    request annotates before the secret exists, which is pure noise
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"

SHARD_JOB = "test"
PYTEST_STEP_ID = "pytest-shard"
UPLOAD_STEP_ID = "mergify-insights"
ACTION_REPO = "mergifyio/gha-mergify-ci"

# A matrix expression has to appear in both filenames and in job_name, or the
# five shards collide. This is the substring every one of them must carry.
SHARD_EXPR = "matrix.shard"


def _load_ci() -> dict:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))


def _shard_job() -> dict:
    return _load_ci()["jobs"][SHARD_JOB]


def _steps() -> list[dict]:
    return _shard_job()["steps"]


def _step_by_id(step_id: str) -> dict:
    for step in _steps():
        if step.get("id") == step_id:
            return step
    raise AssertionError(
        f"ci.yml job '{SHARD_JOB}' has no step with id '{step_id}'. "
        "The CI Insights wiring is addressed by id, so renaming one without "
        "the other turns the check into one that cannot fire."
    )


def _pytest_run_block() -> str:
    return _step_by_id(PYTEST_STEP_ID)["run"]


def test_pytest_shard_writes_a_junit_report() -> None:
    """if the pytest step drops --junitxml then nothing is ever measured."""
    run = _pytest_run_block()
    assert "--junitxml=" in run, (
        "The pytest fast lane no longer writes a JUnit report, so the CI "
        "Insights upload has nothing to send and this lane records no test "
        "health at all -- while every step still reads as green."
    )


def _junitxml_path() -> str:
    match = re.search(r'--junitxml="([^"]+)"', _pytest_run_block())
    assert match, (
        "--junitxml is present but not in the quoted form this test reads. "
        "Keep it as --junitxml=\"<path>\" so the report path stays greppable "
        "and can be compared against the upload's report_path."
    )
    return match.group(1)


def test_junit_report_path_matches_the_upload_report_path() -> None:
    """if the two disagree then the upload finds nothing, and looks fine."""
    written = _junitxml_path()
    uploaded = _step_by_id(UPLOAD_STEP_ID)["with"]["report_path"]
    assert written == uploaded, (
        f"pytest writes {written!r} but the CI Insights step uploads "
        f"{uploaded!r}. Both halves look correctly configured in isolation, "
        "so this fails as an empty dashboard rather than as a red check."
    )


def test_junit_report_path_is_per_shard() -> None:
    """if the path stops varying then five shards race on one file."""
    written = _junitxml_path()
    assert SHARD_EXPR in written, (
        f"The JUnit report path {written!r} does not vary per shard, so all "
        "five shards write the same file on their own runners and only one "
        "shard's results can ever be attributed correctly."
    )


def test_upload_step_names_the_job_per_shard() -> None:
    """if job_name is dropped then all five shards file under one name."""
    with_block = _step_by_id(UPLOAD_STEP_ID)["with"]
    assert "job_name" in with_block, (
        "job_name is missing. The action defaults the test-job name to the "
        "GitHub job name, which is identical across all five shards, so "
        "every shard's results are filed under one name and the per-test "
        "history this lane exists to produce is worthless. The action's own "
        "input documentation calls this out for matrix jobs."
    )
    assert SHARD_EXPR in with_block["job_name"], (
        f"job_name {with_block['job_name']!r} does not vary per shard, which "
        "fails exactly the same way as omitting it, and just as silently."
    )


def test_upload_step_passes_the_test_runner_outcome() -> None:
    """if test_step_outcome is dropped then silent failures stop being seen."""
    with_block = _step_by_id(UPLOAD_STEP_ID)["with"]
    outcome = with_block.get("test_step_outcome", "")
    assert outcome, (
        "test_step_outcome is missing, so MERGIFY_TEST_EXIT_CODE is never "
        "set and the CLI cannot flag a silent failure: a runner that exited "
        "non-zero while its JUnit report carries no failures. That is the "
        "single defect this lane was wired first to catch, and without this "
        "input uploads keep succeeding so nothing ever looks wrong."
    )
    assert f"steps.{PYTEST_STEP_ID}.outcome" in outcome, (
        f"test_step_outcome is {outcome!r}, which does not read the pytest "
        f"step's own outcome (steps.{PYTEST_STEP_ID}.outcome). An expression "
        "naming a step that does not exist resolves to an empty string, and "
        "the action treats empty the same as omitted -- so detection would "
        "be off with no error raised anywhere."
    )


def test_upload_step_cannot_redden_the_lane() -> None:
    """if it loses continue-on-error then a Mergify outage reddens PRs."""
    step = _step_by_id(UPLOAD_STEP_ID)
    assert step.get("continue-on-error") is True, (
        "The CI Insights step must stay continue-on-error. Round 1 is "
        "measure-only: pytest's own exit code is the shard's verdict. "
        "Without this, a Mergify outage, a rate limit or an expired token "
        "paints every pull request red on a lane that is only recording."
    )


def test_upload_step_is_pinned_to_a_sha() -> None:
    """if the action is pinned to a tag then a third party can change CI."""
    uses = _step_by_id(UPLOAD_STEP_ID)["uses"]
    assert uses.startswith(f"{ACTION_REPO}@"), uses
    ref = uses.split("@", 1)[1]
    assert re.fullmatch(r"[0-9a-f]{40}", ref), (
        f"The CI Insights action is pinned to {ref!r}, not a full commit "
        "SHA. A moving tag lets a third party change what executes inside "
        "this repo's CI without any review here."
    )


def _guard_step() -> dict:
    for step in _steps():
        if "must have recorded something" in step.get("name", ""):
            return step
    raise AssertionError(
        "The CI Insights upload-verdict guard is gone. Without it a "
        "'rejected' upload records NO test data while the step still reads "
        "as having run, so the flake baseline is quietly built from missing "
        "runs -- a green step certifying an empty measurement, which is the "
        "defect .claude/rules/verification.md is written against."
    )


def test_guard_separates_rejected_from_failed_from_unknown() -> None:
    """if the guard collapses these then a scope bug reads like a blip."""
    run = _guard_step()["run"]
    for verdict in ("rejected", "failed"):
        assert f"{verdict})" in run, (
            f"The guard no longer handles a '{verdict}' upload outcome. "
            "'rejected' is permanent and means nothing was recorded (usually "
            "a token without CI Insights scope); 'failed' is transient. "
            "Collapsing them makes a persistent misconfiguration read like a "
            "blip nobody needs to act on."
        )
    assert "UNKNOWN" in run, (
        "The guard no longer reports an empty upload outcome as UNKNOWN. An "
        "outcome that could not be read is not the same as a successful "
        "upload, and must never render as one."
    )
    assert "::error" in run, (
        "The guard no longer emits an ::error annotation. A rejected upload "
        "has to be visible on the pull request; discovering it weeks later "
        "when the dashboard answers a question from no data is the failure "
        "this guard exists to prevent."
    )


def test_token_is_on_the_job_so_the_guards_can_fire() -> None:
    """if the token leaves job env then unconfigured looks like broken."""
    job_env = _shard_job().get("env", {})
    assert "MERGIFY_TOKEN" in job_env, (
        "MERGIFY_TOKEN must stay in the job env. A secret cannot be read "
        "from a step `if:` condition, and these steps gate on it so that an "
        "unprovisioned token (this lane is NOT MEASURED, which is honest) "
        "renders differently from a token that is present and rejected."
    )
    assert "secrets.MERGIFY_TOKEN" in job_env["MERGIFY_TOKEN"], job_env["MERGIFY_TOKEN"]


def test_ci_insights_steps_are_skipped_without_a_token() -> None:
    """if they stop gating then every PR annotates before the secret exists."""
    for step in (_step_by_id(UPLOAD_STEP_ID), _guard_step()):
        condition = step.get("if", "")
        assert "env.MERGIFY_TOKEN != ''" in condition, (
            f"Step {step.get('name')!r} no longer skips when the token is "
            "unset. On forks and before the secret is provisioned this would "
            "annotate every shard of every pull request, which is noise on a "
            "lane already fighting red CI."
        )
        assert "always()" in condition, (
            f"Step {step.get('name')!r} lost always(). The runs most worth "
            "recording are the RED ones; a step that only runs on success "
            "records exactly the wrong half of the history."
        )
