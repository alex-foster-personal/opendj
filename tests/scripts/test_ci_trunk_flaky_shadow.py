"""The pytest fast lane's JUnit reports reach Trunk Flaky Tests, one upload per shard.

Wired as a shadow on Sun 27 Sep 2026 (ADR-NEW-trunk-flaky-tests-shadow); the only
test-health uploader since Wed 30 Sep 2026 (ADR-NEW-trunk-is-the-only-merge-queue).
The isolated `ci-insights` job downloads the five shards' staged reports and
uploads each on its own, in one job, so it adds no extra billing minimum. It is
MEASURE ONLY: Trunk's
quarantine (which can turn a failing test run green) stays off until the maintainer
decides otherwise, and the upload can never be the lane's verdict.

Regression lines:
  - if the Trunk upload moves into a job that checks out repository code then
    TRUNK_API_TOKEN is readable by the test suite and anything it spawns
  - if the upload step loses continue-on-error then a Trunk outage paints every
    pull request red on a lane that is only supposed to be measuring
  - if quarantine is switched on in the uploader then Trunk's CLI turns ANY
    non-zero exit green once every recorded failure is quarantined, timeouts
    included; the verdict lives in the test job instead
  - if the action stops being pinned to a full commit SHA then a third party
    can change what runs inside this repo's CI without review
  - if use-uncloned-repo is dropped then the uploader looks for a git checkout
    this job deliberately does not have, and every upload fails
  - if the shards are uploaded together again then one dead shard is ~20% of the
    upload, under Trunk's 80% infrastructure-failure threshold, and every test
    in it is flagged flaky (Wed 30 Sep 2026, 1787 false flags;
    ADR-NEW-trunk-flaky-quarantine-on)
  - if the guard is dropped then a failed upload reads the same as a success
  - if the guard stops counting report files then an upload given zero reports
    (the uploader allows missing files) prints TRUNK_UPLOAD: success while
    Trunk recorded nothing
  - if the steps stop gating on the token then every run annotates before the
    secret exists, which is pure noise
  - if the probe exports the token's value then every later step can read it
  - if a failed download reads as a missing report then the instrumentation
    dies silently behind a green step that measured nothing
  - if the retired queue's uploader or its secret comes back into ci.yml then a
    credential for an uninstalled app is wired into CI again
  - if an upload leans on the uploader's pull_request-only defaults for the repo
    url, head sha or head branch then every main push and dispatch upload fails
    with missing required arguments while the job stays green
  - if cli-version is left at latest then a binary chosen at run time runs with
    the token in scope
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"
INSIGHTS_JOB = "ci-insights"
SHARDS = (1, 2, 3, 4, 5)
PROBE_STEP_ID = "trunk-token"
GUARD_NAME = "The Trunk uploads must have run"


# -----------------------------------------------------------------------------
def _jobs() -> dict:
    return yaml.safe_load(CI.read_text())["jobs"]


def _steps(job: str) -> list[dict]:
    return _jobs()[job]["steps"]


def _step_by_id(job: str, step_id: str) -> dict:
    matches = [s for s in _steps(job) if s.get("id") == step_id]
    assert len(matches) == 1, f"{job}: want one step id={step_id}, got {len(matches)}"
    return matches[0]


def _step_by_name(job: str, name: str) -> dict:
    matches = [s for s in _steps(job) if s.get("name") == name]
    assert len(matches) == 1, f"{job}: want one step {name!r}, got {len(matches)}"
    return matches[0]


def _uploads() -> list[dict]:
    return [_step_by_id(INSIGHTS_JOB, f"trunk-flaky-tests-{n}") for n in SHARDS]


# -----------------------------------------------------------------------------
def test_trunk_uploads_live_in_the_isolated_insights_job() -> None:
    for upload in _uploads():
        assert upload["uses"].startswith("trunk-io/analytics-uploader@"), upload["uses"]
    all_uploaders = [
        s for s in _steps(INSIGHTS_JOB) if str(s.get("uses", "")).startswith("trunk-io/")
    ]
    assert len(all_uploaders) == len(SHARDS), "exactly one uploader per shard, no combined upload"


def test_trunk_token_never_shares_a_job_with_repository_code() -> None:
    for name, job in _jobs().items():
        text = yaml.safe_dump(job)
        checks_out = any(
            str(s.get("uses", "")).startswith("actions/checkout@") for s in job.get("steps", [])
        )
        if checks_out:
            assert "TRUNK_API_TOKEN" not in text, f"{name} checks out code and sees the token"
    assert "TRUNK_API_TOKEN" in yaml.safe_dump(_jobs()[INSIGHTS_JOB]), "positive control"


def test_trunk_uploads_cannot_redden_the_lane() -> None:
    for upload in _uploads():
        assert upload.get("continue-on-error") is True, upload["id"]


def test_trunk_uploader_never_quarantines() -> None:
    for upload in _uploads():
        assert str(upload["with"].get("quarantine")).lower() == "false", upload["id"]


def test_trunk_uploads_are_pinned_to_a_sha() -> None:
    for upload in _uploads():
        assert re.fullmatch(r"trunk-io/analytics-uploader@[0-9a-f]{40}", upload["uses"]), upload[
            "uses"
        ]


def test_trunk_uploads_run_without_a_checkout() -> None:
    for upload in _uploads():
        assert str(upload["with"].get("use-uncloned-repo")).lower() == "true"
    assert not any(
        str(s.get("uses", "")).startswith("actions/checkout@") for s in _steps(INSIGHTS_JOB)
    )


def test_each_upload_carries_exactly_its_own_shard_report() -> None:
    download_paths = [
        s["with"]["path"]
        for s in _steps(INSIGHTS_JOB)
        if str(s.get("id", "")).startswith("download-")
    ]
    assert len(download_paths) == len(SHARDS), download_paths
    for shard, (upload, path) in enumerate(zip(_uploads(), download_paths, strict=True), start=1):
        junit = upload["with"]["junit-paths"]
        assert junit == f"{path}/junit-shard-{shard}.xml", junit
        assert "*" not in junit, "a glob could sweep several shards into one upload"
        assert f"steps.shard-report-{shard}.outputs.report == 'true'" in upload["if"], upload["if"]


def test_trunk_steps_are_skipped_without_a_token() -> None:
    probe = _step_by_id(INSIGHTS_JOB, PROBE_STEP_ID)
    assert probe["env"] == {"TRUNK_API_TOKEN": "${{ secrets.TRUNK_API_TOKEN }}"}
    gated = [s for s in _steps(INSIGHTS_JOB) if s.get("id") != PROBE_STEP_ID]
    assert len(gated) == len(_steps(INSIGHTS_JOB)) - 1, "positive control: the probe was found"
    checked = {s["name"] for s in gated}
    assert {u["name"] for u in _uploads()} | {GUARD_NAME} <= checked, (
        "control: the uploads and the guard are among the steps checked"
    )
    for step in gated:
        assert "steps.trunk-token.outputs.present == 'true'" in step["if"], step["if"]
        assert "always()" in step["if"], f"{step.get('name')!r}: red runs are the ones to record"
        assert "mergify" not in step["if"], step["if"]


def test_the_probe_exports_only_a_boolean() -> None:
    run = _step_by_id(INSIGHTS_JOB, PROBE_STEP_ID)["run"]
    assert "present=true" in run and "present=false" in run, run
    assert "$TRUNK_API_TOKEN" not in run.replace("${TRUNK_API_TOKEN:-}", ""), run


def test_the_retired_queue_is_gone_from_ci() -> None:
    text = CI.read_text()
    assert "trunk-io/analytics-uploader@" in text, "positive control: ci.yml was read"
    for retired in ("gha-mergify-ci", "MERGIFY_TOKEN", "mergify-token", "mergify-insights"):
        assert retired not in text, f"{retired!r} is back in ci.yml"


def test_trunk_guard_reads_every_shard_upload() -> None:
    guard = _step_by_name(INSIGHTS_JOB, GUARD_NAME)
    for n in SHARDS:
        assert (
            guard["env"][f"TRUNK_UPLOAD_{n}"] == f"${{{{ steps.trunk-flaky-tests-{n}.outcome }}}}"
        )
        assert (
            guard["env"][f"HAS_REPORT_{n}"] == f"${{{{ steps.shard-report-{n}.outputs.report }}}}"
        )
        assert guard["env"][f"DOWNLOAD_{n}"] == f"${{{{ steps.download-{n}.outcome }}}}"


def _run_guard(
    results: dict[int, tuple[str, str]], downloads: dict[int, str] | None = None
) -> str:
    """Execute the guard's own run block; results maps shard -> (has_report, upload outcome).

    Every download succeeded unless `downloads` says otherwise for a shard.
    """
    guard = _step_by_name(INSIGHTS_JOB, GUARD_NAME)
    bash = shutil.which("bash")
    assert bash, "bash is required to execute the guard"
    env = {"PATH": "/usr/bin:/bin"}
    for n in SHARDS:
        has_report, upload = results.get(n, ("false", ""))
        env[f"HAS_REPORT_{n}"], env[f"TRUNK_UPLOAD_{n}"] = has_report, upload
        env[f"DOWNLOAD_{n}"] = (downloads or {}).get(n, "success")
    done = subprocess.run(
        [bash, "-e", "-c", guard["run"]],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return done.stdout


def test_trunk_guard_reports_zero_reports_as_unmeasured() -> None:
    out = _run_guard({})
    assert "::warning title=Trunk UNMEASURED" in out, out
    assert "TRUNK_UPLOAD: success" not in out, out


def test_trunk_guard_counts_successful_shard_uploads() -> None:
    out = _run_guard({1: ("true", "success"), 3: ("true", "success")})
    assert "TRUNK_UPLOAD: success (2 of 5 shard uploads)" in out, out
    assert "UNMEASURED" not in out, out


def test_trunk_guard_names_a_failed_shard_upload() -> None:
    out = _run_guard({1: ("true", "success"), 4: ("true", "failure")})
    assert "Trunk upload UNMEASURED (shard 4 of 5)" in out and "outcome=failure" in out, out
    assert "TRUNK_UPLOAD: success (1 of 5 shard uploads)" in out, out


def test_trunk_guard_reports_a_failed_download_as_unmeasured_not_absent() -> None:
    out = _run_guard({1: ("true", "success")}, downloads={2: "failure", 3: ""})
    assert "Trunk upload UNMEASURED (shard 2 of 5)" in out, out
    assert "download outcome=failure" in out and "download outcome=empty" in out, out
    assert "no report (shard 2 of 5)" not in out, out
    assert "no report (shard 4 of 5)" in out, "control: a clean download still reads as absent"
    assert "TRUNK_UPLOAD: success (1 of 5 shard uploads)" in out, out


#: Inputs whose uploader defaults read github.event.pull_request only, so they are
#: empty on a push or dispatch. Each needs a pull_request value AND a fallback.
HEAD_INPUTS = {
    "gh-repo-url": "github.repository",
    "gh-repo-head-sha": "github.sha",
    "gh-repo-head-branch": "github.ref_name",
    "gh-repo-head-commit-epoch": "github.event.head_commit.timestamp",
}


def test_uploads_name_the_repo_and_head_on_every_event() -> None:
    for upload in _uploads():
        for name, fallback in HEAD_INPUTS.items():
            value = str(upload["with"].get(name, ""))
            assert "github.event.pull_request." in value, (upload["id"], name, value)
            assert fallback in value, (upload["id"], name, value)


def test_trunk_cli_version_is_pinned() -> None:
    for upload in _uploads():
        version = str(upload["with"].get("cli-version", "latest"))
        assert re.fullmatch(r"\d+\.\d+\.\d+", version), (upload["id"], version)

