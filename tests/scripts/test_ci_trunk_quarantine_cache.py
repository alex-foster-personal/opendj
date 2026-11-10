"""Trunk quarantine survives an Actions budget wall (ADR-NEW-trunk-quarantine-survives-a-budget-wall).

Thu 15 Oct 2026 the GitHub Actions budget ran out, every hosted job stopped
starting, and with it the hosted `trunk-quarantine-list` job: the self-hosted
shards kept running with an empty list, so nothing was excused. the maintainer: "Trunks
quarantine - how is that off if actions is out of budget?"

  - [if] the hosted fetch cannot run [then] shards read main's newest cached list, [else stop].

Regression lines:
  - if a live list is not passed through and cached then a fresh quarantine never reaches the shards
  - if no live list and no cache yields a list then an empty runner excuses by accident
  - if a cached list over the age cap is used then a quarantine Trunk lifted long ago is still excused
  - if a cached list dated in the future or unreadable is used then a bad clock or a torn file excuses
  - if a file left in a shared workspace is read without a cache hit then another job's list is trusted
  - if the cache job holds the token or leaves the self-hosted pool then the fix moved the secret or the bill
  - if pull requests start the token fetch again then the org token runs on every PR run
  - if the fetch regains a hosted runner or fallback then the budget refuses it and main reads red
  - if the resolver goes back to inline shell then CI depends on bash and jq again (AGENTS.md)
  - if a pull request run saves the cache then a branch could seed a list (it is PR-scoped, but pointless)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from tests.scripts.ci_runner_routes import TOKEN_RUNS_ON_LINUX

REPO_ROOT = Path(__file__).resolve().parents[2]
#: ci.yml carried the same pair until Wed 4 Nov 2026 (ADR-NEW-trunk-removed-from-pr-ci).
WORKFLOWS = {
    "e2e.yml": "trunk-quarantine-e2e-",
}
CACHE_JOB = "trunk-quarantine"
LIST_JOB = "trunk-quarantine-list"
# The interpreter is resolved once, never a bare python3 in argv (AGENTS.md).
RESOLVE_RUN = (
    '# Resolved once, never a bare python3 in argv (AGENTS.md, "Portable by default").\n'
    'py="$(command -v python3)" || { echo "::error title=Trunk quarantine::no python3 on PATH"; exit 1; }\n'
    'exec "$py" -m scripts.ci_trunk_quarantine_cache'
)
LIVE = '{"status": "ok", "tests": [{"c": "tests.a", "n": "test_flaky"}]}'
CACHED_LIST = {"status": "ok", "tests": [{"c": "tests.b", "n": "test_old_flaky"}]}

BASH = shutil.which("bash")
pytestmark = [
    pytest.mark.requirement("CIQUAR-02"),
    pytest.mark.skipif(
        BASH is None,
        reason="UNAVAILABLE: no bash on this host, so the job's own run block cannot be executed",
    ),
]


def _jobs(workflow: str) -> dict:
    return yaml.safe_load((REPO_ROOT / ".github" / "workflows" / workflow).read_text())["jobs"]


def _step(workflow: str, step_id: str) -> dict:
    steps = [s for s in _jobs(workflow)[CACHE_JOB]["steps"] if s.get("id") == step_id]
    assert len(steps) == 1, (workflow, step_id, len(steps))
    return steps[0]


def _resolve(
    workflow: str,
    tmp_path: Path,
    *,
    live: str = "",
    live_result: str = "skipped",
    cache_hit: str = "false",
    cached: dict | str | None = None,
) -> tuple[int, dict[str, str], str, Path]:
    step = _step(workflow, "resolve")
    assert step["run"].strip() == RESOLVE_RUN, step["run"]
    work = tmp_path / "work"
    work.mkdir()
    cache_file = work / step["env"]["CACHE_FILE"]
    if cached is not None:
        cache_file.parent.mkdir(parents=True)
        raw = cached if isinstance(cached, str) else json.dumps(cached)
        cache_file.write_text(raw, encoding="utf-8", newline="\n")
    output = tmp_path / "github_output"
    output.write_text("", encoding="utf-8", newline="\n")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(REPO_ROOT),
        "GITHUB_OUTPUT": str(output),
        "LIVE_LIST": live,
        "LIVE_RESULT": live_result,
        "CACHE_HIT": cache_hit,
        "CACHE_FILE": step["env"]["CACHE_FILE"],
        "MAX_AGE_HOURS": step["env"]["MAX_AGE_HOURS"],
    }
    # Run the job's own bash block, so its command resolution and exec are under test.
    # `command -v python3` finds this interpreter first, through a shim directory.
    shim = tmp_path / "bin"
    shim.mkdir()
    (shim / "python3").symlink_to(sys.executable)
    env["PATH"] = f"{shim}{os.pathsep}{env['PATH']}"
    bash = BASH
    assert bash is not None  # pytestmark skipif guarantees bash at runtime
    done = subprocess.run(
        [bash, "-c", step["run"]],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    outputs = dict(
        line.split("=", 1) for line in output.read_text().splitlines() if "=" in line
    )
    return done.returncode, outputs, done.stdout + done.stderr, cache_file


def _cached(age_hours: float) -> dict:
    epoch = int(time.time() - age_hours * 3600)
    return {"fetched_at": "then", "fetched_epoch": epoch, "list": CACHED_LIST}


# ----- the run block ----------------------------------------------------------
@pytest.mark.parametrize("workflow", sorted(WORKFLOWS))
def test_a_live_list_passes_through_and_is_written_for_the_cache(
    workflow: str, tmp_path: Path
) -> None:
    rc, out, log, cache_file = _resolve(workflow, tmp_path, live=LIVE, live_result="success")
    assert rc == 0, log
    assert out["source"] == "live"
    assert json.loads(out["list"]) == json.loads(LIVE)
    written = json.loads(cache_file.read_text())
    assert written["list"] == json.loads(LIVE)
    assert abs(written["fetched_epoch"] - time.time()) < 120


DEFAULT_BRANCH_ONLY = "github.ref == format('refs/heads/{0}', github.event.repository.default_branch)"

@pytest.mark.parametrize("workflow", sorted(WORKFLOWS))
def test_a_fresh_cached_list_is_used_when_the_fetch_did_not_run(
    workflow: str, tmp_path: Path
) -> None:
    rc, out, log, _ = _resolve(
        workflow, tmp_path, live_result="failure", cache_hit="true", cached=_cached(30)
    )
    assert rc == 0, log
    assert out["source"] == "cached"
    assert json.loads(out["list"]) == CACHED_LIST
    assert "::warning" in log and "30h old" in log, log


def test_a_pull_request_reading_a_recent_cache_is_a_notice_not_a_warning(tmp_path: Path) -> None:
    rc, out, log, _ = _resolve("e2e.yml", tmp_path, cache_hit="true", cached=_cached(2))
    assert rc == 0 and out["source"] == "cached", log
    assert "::notice" in log and "::warning" not in log, log


@pytest.mark.parametrize(
    "case",
    ["no-cache", "stale", "future", "bad-epoch", "torn", "not-ok", "no-cache-hit"],
)
def test_no_usable_list_excuses_nothing(case: str, tmp_path: Path) -> None:
    cached: dict | str | None = _cached(1)
    hit = "true"
    if case == "no-cache":
        cached, hit = None, "false"
    elif case == "stale":
        cached = _cached(169)
    elif case == "future":
        cached = _cached(-2)
    elif case == "bad-epoch":
        cached = {"fetched_epoch": "yesterday", "list": CACHED_LIST}
    elif case == "torn":
        cached = json.dumps(_cached(1))[:-9]  # a write cut off mid-file
    elif case == "not-ok":
        cached = {**_cached(1), "list": {"status": "error", "tests": []}}
    elif case == "no-cache-hit":
        hit = "false"  # a file another job left in a shared workspace
    rc, out, log, _ = _resolve("e2e.yml", tmp_path, live_result="failure", cache_hit=hit, cached=cached)
    assert rc == 0, log
    assert out["source"] == "none", (case, out, log)
    assert "list" not in out, (case, out)
    assert "nothing is quarantined" in log, log


def test_a_successful_job_with_an_unusable_list_fails_closed(tmp_path: Path) -> None:
    # The cache stands in for a job that could not run, never for one that ran and
    # returned garbage (Sol P1 on 1b5a5b5df).
    rc, out, log, _ = _resolve(
        "e2e.yml",
        tmp_path,
        live='{"status": "error"}',
        live_result="success",
        cache_hit="true",
        cached=_cached(1),
    )
    assert rc == 0 and out["source"] == "none" and "list" not in out, log
    assert "unusable" in log, log


@pytest.mark.parametrize("live_result", ["failure", "cancelled", "skipped"])
def test_a_job_that_did_not_succeed_falls_back_to_the_cache(
    tmp_path: Path, live_result: str
) -> None:
    rc, out, log, _ = _resolve(
        "e2e.yml",
        tmp_path,
        live="",
        live_result=live_result,
        cache_hit="true",
        cached=_cached(1),
    )
    assert rc == 0 and out["source"] == "cached", log


@pytest.mark.parametrize("live_result", ["failure", "cancelled", "skipped"])
def test_a_usable_list_from_a_fetch_that_did_not_succeed_is_not_trusted(
    tmp_path: Path, live_result: str
) -> None:
    # The fetch step is continue-on-error, so it can write an output and then fail
    # (Sol P1 on 069397bdd). Only the verified cache may stand in, never that list.
    rc, out, log, cache_file = _resolve(
        "e2e.yml", tmp_path, live=LIVE, live_result=live_result, cache_hit="false"
    )
    assert rc == 0 and out["source"] == "none" and "list" not in out, log
    assert not cache_file.exists(), "an untrusted live list must not be cached"


def test_the_time_shown_comes_from_the_epoch_not_fetched_at(tmp_path: Path) -> None:
    # Sol P1 on 08cbe888d: fetched_at is free text, so a file whose fetched_at
    # disagrees with its epoch must not be able to put a false date in the log.
    doc = _cached(1)
    doc["fetched_at"] = "2099-01-01T00:00:00+0000"
    rc, out, log, _ = _resolve(
        "e2e.yml", tmp_path, live="", live_result="failure", cache_hit="true", cached=doc
    )
    assert rc == 0 and out["source"] == "cached", log
    shown = time.strftime("%Y-%m-%dT%H:", time.gmtime(doc["fetched_epoch"]))
    assert shown in log and "2099" not in log, log


@pytest.mark.parametrize(
    ("live_result", "cache_hit"),
    [("", "true"), ("succes", "true"), ("failure", ""), ("failure", "yes")],
)
def test_a_bad_input_is_a_configuration_error(
    tmp_path: Path, live_result: str, cache_hit: str
) -> None:
    # Sol P1 on 08cbe888d: a typo or a missing wire must not read as an outage.
    rc, out, log, _ = _resolve(
        "e2e.yml", tmp_path, live="", live_result=live_result, cache_hit=cache_hit,
        cached=_cached(1),
    )
    assert rc == 2 and "bad configuration" in log and "list" not in out, log


# ----- the wiring ---------------------------------------------------------------
@pytest.mark.parametrize("workflow", sorted(WORKFLOWS))
def test_the_cache_job_is_self_hosted_and_holds_no_secret(workflow: str) -> None:
    job = _jobs(workflow)[CACHE_JOB]
    # e2e.yml's scope job left the light pool for the e2e seats on Wed 4 Nov 2026
    # (ADR-NEW-e2e-after-ci-gate); the cache job keeps the light chain its verdict uses.
    light = _jobs(workflow)["verdict" if workflow == "e2e.yml" else "scope"]["runs-on"]
    assert job["runs-on"] == light, "the light self-hosted pool"
    assert "ubuntu-latest\"') }}" in job["runs-on"] and "vars.CI_RUNS_ON_LINUX" in job["runs-on"]
    assert "secrets." not in yaml.safe_dump(job)
    assert job["needs"] == LIST_JOB
    assert "!cancelled()" in job["if"] and "vars.TRUNK_QUARANTINE_CI == 'true'" in job["if"]
    assert job["outputs"]["list"] == "${{ steps.resolve.outputs.list }}"
    assert job["permissions"] == {"contents": "read"}


@pytest.mark.parametrize("workflow", sorted(WORKFLOWS))
def test_the_workspace_is_cleared_before_the_restore(workflow: str) -> None:
    steps = _jobs(workflow)[CACHE_JOB]["steps"]
    # Root-owned debris would fail the checkout itself (test_workspace_ownership_hygiene).
    assert steps[0]["name"] == "Reclaim workspace ownership before checkout"
    assert steps[1]["uses"].startswith("actions/checkout@")
    assert steps[1]["with"]["persist-credentials"] is False
    assert steps[2]["run"].strip() == "scripts/ci_clean_untracked.sh"  # git clean -ffdx
    assert steps[3]["run"].strip() == "scripts/ci_runner_preflight.sh python3"
    assert steps[4]["id"] == "restore"
    resolve = _step(workflow, "resolve")
    assert resolve["env"]["CACHE_HIT"] == "${{ steps.restore.outputs.cache-matched-key != '' }}"
    # The fetch step is continue-on-error, so the job result alone reads success
    # when Trunk failed; the step outcome must win (Codex P1 on ed7bbaf5e).
    assert resolve["env"]["LIVE_RESULT"] == (
        "${{ needs.trunk-quarantine-list.outputs.fetch_outcome"
        " || needs.trunk-quarantine-list.result }}"
    )
    assert _jobs(workflow)[LIST_JOB]["outputs"]["fetch_outcome"] == "${{ steps.fetch.outcome }}"


@pytest.mark.parametrize(("workflow", "prefix"), sorted(WORKFLOWS.items()))
def test_restore_and_save_share_one_prefix_and_only_main_saves(workflow: str, prefix: str) -> None:
    steps = _jobs(workflow)[CACHE_JOB]["steps"]
    restore = _step(workflow, "restore")
    assert restore["uses"].startswith("actions/cache/restore@")
    assert restore["with"]["restore-keys"] == prefix
    assert restore["with"]["key"].startswith(prefix)
    save = steps[-1]
    assert save["uses"].startswith("actions/cache/save@")
    assert save["with"]["key"] == restore["with"]["key"]
    assert save["with"]["path"] == restore["with"]["path"]
    assert "github.event_name != 'pull_request'" in save["if"]
    assert "steps.resolve.outputs.source == 'live'" in save["if"]
    # A dispatch on another ref must not publish a list (Sol P1 on e6603300b).
    assert DEFAULT_BRANCH_ONLY in save["if"]
    # Restore always runs; the resolver decides whether the cache may be used.
    assert "if" not in restore


@pytest.mark.parametrize("workflow", sorted(WORKFLOWS))
def test_only_the_default_branch_fetches_the_live_list(workflow: str) -> None:
    assert DEFAULT_BRANCH_ONLY in _jobs(workflow)[LIST_JOB]["if"]


@pytest.mark.parametrize("workflow", sorted(WORKFLOWS))
def test_pull_requests_do_not_start_the_token_fetch(workflow: str) -> None:
    # Self-hosted, no hosted fallback (ADR-NEW-trunk-token-jobs-self-hosted).
    job = _jobs(workflow)[LIST_JOB]
    assert job["runs-on"] == TOKEN_RUNS_ON_LINUX
    assert "github.event_name != 'pull_request'" in job["if"]
