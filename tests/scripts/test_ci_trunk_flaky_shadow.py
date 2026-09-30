"""The pytest fast lane's JUnit reports reach Trunk Flaky Tests, one upload per shard.

the maintainer, Sun 27 Sep 2026: evaluate Trunk alongside Mergify before any cutover.
The shadow reuses the reports the isolated `ci-insights` job already downloads,
so it adds no job and no extra billing minimum. It is MEASURE ONLY: Trunk's
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
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
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
    all_uploaders = [s for s in _steps(INSIGHTS_JOB) if str(s.get("uses", "")).startswith("trunk-io/")]
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
        assert re.fullmatch(r"trunk-io/analytics-uploader@[0-9a-f]{40}", upload["uses"]), upload["uses"]


def test_trunk_uploads_run_without_a_checkout() -> None:
    for upload in _uploads():
        assert str(upload["with"].get("use-uncloned-repo")).lower() == "true"
    assert not any(
        str(s.get("uses", "")).startswith("actions/checkout@") for s in _steps(INSIGHTS_JOB)
    )


def test_each_upload_carries_exactly_its_own_shard_report() -> None:
    download_paths = [
        s["with"]["path"] for s in _steps(INSIGHTS_JOB) if str(s.get("id", "")).startswith("download-")
    ]
    assert len(download_paths) == len(SHARDS), download_paths
    for shard, (upload, path) in enumerate(zip(_uploads(), download_paths, strict=True), start=1):
        junit = upload["with"]["junit-paths"]
        assert junit == f"{path}/junit-shard-{shard}.xml", junit
        assert "*" not in junit, "a glob could sweep several shards into one upload"
        assert f"steps.shard-outcome-{shard}.outputs.report == 'true'" in upload["if"], upload["if"]


def test_trunk_steps_are_skipped_without_a_token() -> None:
    probe = _step_by_id(INSIGHTS_JOB, PROBE_STEP_ID)
    assert probe["env"] == {"TRUNK_API_TOKEN": "${{ secrets.TRUNK_API_TOKEN }}"}
    for step in (*_uploads(), _step_by_name(INSIGHTS_JOB, GUARD_NAME)):
        assert "steps.trunk-token.outputs.present == 'true'" in step["if"], step["if"]


def test_trunk_guard_reads_every_shard_upload() -> None:
    guard = _step_by_name(INSIGHTS_JOB, GUARD_NAME)
    for n in SHARDS:
        assert guard["env"][f"TRUNK_UPLOAD_{n}"] == f"${{{{ steps.trunk-flaky-tests-{n}.outcome }}}}"
        assert guard["env"][f"HAS_REPORT_{n}"] == f"${{{{ steps.shard-outcome-{n}.outputs.report }}}}"


def _run_guard(results: dict[int, tuple[str, str]]) -> str:
    """Execute the guard's own run block; results maps shard -> (has_report, upload outcome)."""
    guard = _step_by_name(INSIGHTS_JOB, GUARD_NAME)
    bash = shutil.which("bash")
    assert bash, "bash is required to execute the guard"
    env = {"PATH": "/usr/bin:/bin"}
    for n in SHARDS:
        has_report, upload = results.get(n, ("false", ""))
        env[f"HAS_REPORT_{n}"], env[f"TRUNK_UPLOAD_{n}"] = has_report, upload
    done = subprocess.run(
        [bash, "-e", "-c", guard["run"]], env=env, capture_output=True, text=True, timeout=30, check=True
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
