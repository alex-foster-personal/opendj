"""Trunk quarantine on the e2e gate's root Playwright suite (CIQUAR-01).

ADR-NEW-trunk-flaky-quarantine-on. The root step records its exit code and
JUnit report; the following verdict passes that step only when every failed
testcase is on Trunk collection L3VoLxNK. The list is fetched by a job on the
self-hosted light pool (no hosted fallback) that never checks out and never
runs a repository file;
the token never appears on a job that checks out or on the gate job. The report
is uploaded by a job on the self-hosted light pool (no hosted fallback,
ADR-NEW-trunk-token-jobs-self-hosted) that is not a merge gate, never checks
out, and stages the report under runner.temp, which the runner empties at the
start of each job.

  - [if] an unlisted root test fails [then] the e2e gate fails, [else stop].

Regression lines:
  - if an unlisted root failure passes then quarantine is hiding a real regression
  - if a failure whose every failed test is listed still fails then quarantine does nothing
  - if a cancelled or skipped root step passes then a suite that never ran reads green
  - if the verdict step loses always() or gains continue-on-error then a red root suite reads green
  - if the list job regains a hosted runner or fallback then the budget refuses it
  - if the token appears on a checkout job or the gate job then the org token meets
    PR code
  - if the token appears in the gate job then the suite can read the org token
  - if the list job checks out or runs a repository file then the org token meets PR code
  - if e2e-trunk-upload is in verdict.needs then an upload failure reddens the merge gate
  - if e2e-trunk-upload regains a hosted runner or fallback then the budget refuses it
  - if e2e-trunk-upload stages its report outside runner.temp then an earlier run's report
    is recorded under this SHA
  - if the e2e or ci list client calls /v2/test-cases then the fetch 404s and excuses nothing
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.scripts.ci_runner_routes import TOKEN_RUNS_ON_LINUX

REPO_ROOT = Path(__file__).resolve().parents[2]
E2E = REPO_ROOT / ".github" / "workflows" / "e2e.yml"
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"
VERDICT_SCRIPT = REPO_ROOT / "scripts" / "ci_trunk_quarantine_verdict.py"
LIST_JOB = "trunk-quarantine-list"
UPLOAD_JOB = "e2e-trunk-upload"
VERDICT_STEP = "Root Playwright verdict (Trunk quarantine applied)"
JUNIT_NAME = "junit-e2e-root.xml"
COLLECTION = "L3VoLxNK"
R2_ACTION = "./.github/actions/r2-artifact"
UPLOADER_SHA = "trunk-io/analytics-uploader@14c0f18990953eecd01c9c5afe3cc63a73b6c371"

pytestmark = pytest.mark.requirement("CIQUAR-01")

TOAST = (
    "toast-tray.spec.ts",
    "[chromium] a pointer over a toast holds it past its dismissal delay",
)
OTHER = ("other.spec.ts", "[chromium] some other title")


def _jobs(workflow: Path = E2E) -> dict:
    return yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"]


def _step(job: str, *, step_id: str | None = None, name: str | None = None) -> dict:
    steps = [
        s for s in _jobs()[job]["steps"] if (step_id and s.get("id") == step_id) or (name and s.get("name") == name)
    ]
    assert len(steps) == 1, f"{job}: want one step id={step_id} name={name}, got {len(steps)}"
    return steps[0]


def _needs(job: dict) -> list[str]:
    needs = job.get("needs") or []
    return [needs] if isinstance(needs, str) else list(needs)


def _checks_out(job: dict) -> bool:
    return any(str(step.get("uses", "")).startswith("actions/checkout@") for step in job.get("steps") or [])


#: Same detector as tests/scripts/test_trunk_queue.py. Duplicated so this file
#: does not import a private helper, and that test is not weakened.
_RUNS_REPO_CODE = re.compile(r"(python3? -m |uv run|\bjust\b|\./|bash scripts/|pnpm|npm )")


def _runs_branch_code(job: dict) -> bool:
    for step in job.get("steps", []):
        if str(step.get("uses", "")).startswith("actions/checkout@"):
            return True
        if _RUNS_REPO_CODE.search(step.get("run") or ""):
            return True
    return False


def _fetch_run(workflow: Path) -> str:
    steps = [s for s in _jobs(workflow)[LIST_JOB]["steps"] if s.get("id") == "fetch"]
    assert len(steps) == 1, workflow
    return steps[0]["run"]


def _junit(path: Path) -> None:
    classname, name = TOAST
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<testsuites><testsuite name="playwright">'
        f'<testcase classname="{classname}" name="{name}" time="0.1">'
        '<failure message="boom">trace</failure>'
        "</testcase></testsuite></testsuites>\n",
        encoding="utf-8",
        newline="\n",
    )


def _listed(*keys: tuple[str, str], status: str = "ok") -> str:
    tests = ",".join(f'{{"c": "{c}", "n": "{n}"}}' for c, n in keys)
    return f'{{"status": "{status}", "tests": [{tests}]}}'


def _run_verdict_step(tmp_path: Path, outcome: str, rc: str, quarantine: str) -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip(
            "runs the gate's verdict step as written, a bash run block on a Linux runner; this host "
            "has no bash. The verdict script itself is covered by test_ci_trunk_quarantine.py."
        )
        raise AssertionError("unreachable: pytest.skip raises")  # narrows bash for the isolated mypy gate
    step = _step("gate", name=VERDICT_STEP)
    runner_temp = tmp_path / "runner_temp"
    runner_temp.mkdir()
    _junit(runner_temp / JUNIT_NAME)
    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    # A launcher, not a symlink: a Windows checkout cannot create a symlink without a privilege.
    venv_python.write_text(
        f'#!/bin/sh\nexec {shlex.quote(Path(sys.executable).as_posix())} "$@"\n', encoding="utf-8", newline="\n"
    )
    venv_python.chmod(0o755)
    (tmp_path / "scripts").mkdir()
    shutil.copy(VERDICT_SCRIPT, tmp_path / "scripts" / VERDICT_SCRIPT.name)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "RUNNER_TEMP": str(runner_temp),
        "ROOT_OUTCOME": outcome,
        "ROOT_RC": rc,
        "TRUNK_QUARANTINE_LIST": quarantine,
    }
    if "SYSTEMROOT" in os.environ:  # a Windows child process needs it to start
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    return subprocess.run(
        [bash, "-e", "-c", step["run"]],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


# ----- the list job ----------------------------------------------------------
def test_the_list_job_is_self_hosted_and_never_checks_out() -> None:
    job = _jobs()[LIST_JOB]
    assert job["name"] == "Trunk quarantined tests (list)"
    assert job["runs-on"] == TOKEN_RUNS_ON_LINUX
    assert job["timeout-minutes"] == 3
    assert job["if"] == (
        "${{ vars.TRUNK_QUARANTINE_CI == 'true' && github.event_name != 'pull_request'"
        # A workflow_run run's github.ref is main's (ADR-NEW-e2e-after-ci-gate), so the
        # event is excluded by name or the token job would run on every pull request.
        " && github.event_name != 'workflow_run'"
        " && github.ref == format('refs/heads/{0}', github.event.repository.default_branch) }}"
    )
    assert "inputs.tier" not in str(job["if"])
    assert job["outputs"]["list"] == "${{ steps.fetch.outputs.list }}"
    assert not _checks_out(job)
    assert all("uses" not in step for step in job["steps"])
    fetch = _step(LIST_JOB, step_id="fetch")
    assert fetch["continue-on-error"] is True
    assert fetch["timeout-minutes"] == 2
    assert fetch["env"]["TRUNK_TEST_COLLECTION_ID"] == COLLECTION
    assert fetch["env"]["TRUNK_API_TOKEN"] == "${{ secrets.TRUNK_API_TOKEN }}"
    assert fetch["env"]["TRUNK_API_URL"] == "https://api.trunk.io"
    run = fetch["run"]
    assert 'py="$(command -v python3)"' in run and "/usr/bin/python3" not in run
    assert not _RUNS_REPO_CODE.search(run)
    assert not _runs_branch_code(job)
    assert "/v2/test-collections/" in run and "/v2/tests" in run
    assert "/v2/test-cases" not in run
    assert "curl" not in run and "jq" not in run
    assert "continue-on-error" not in _step("gate", name=VERDICT_STEP)


def test_the_e2e_list_client_is_python() -> None:
    """e2e.yml inlines the protocol under a resolved python3.

    An earlier curl client called /v2/test-cases, which Trunk v2 does not have. ci.yml ran
    the same block until Wed 4 Nov 2026 (ADR-NEW-trunk-removed-from-pr-ci)."""
    e2e_run = _fetch_run(E2E)
    assert 'py="$(command -v python3)"' in e2e_run
    assert "/v2/test-collections/" in e2e_run and "/v2/tests" in e2e_run
    assert "/v2/test-cases" not in e2e_run


def test_the_inlined_list_fetch_fails_closed_without_a_checkout(tmp_path: Path) -> None:
    """The run block is the fetch: no workspace files, fail-closed on a dead API."""
    if shutil.which("bash") is None or shutil.which("python3") is None:
        pytest.skip(
            "runs the list step as written (bash, python3 on PATH); this host lacks "
            "one. The portable twin is test_ci_trunk_quarantine.py's list-client tests."
        )
    fetch = _step(LIST_JOB, step_id="fetch")
    output = tmp_path / "github_output"
    output.write_text("")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        closed = sock.getsockname()[1]
    env = {
        "PATH": os.environ.get("PATH", ""),
        "GITHUB_OUTPUT": str(output),
        "TRUNK_API_TOKEN": "not-a-real-token",
        "TRUNK_API_URL": f"http://127.0.0.1:{closed}",
        "TRUNK_TEST_COLLECTION_ID": fetch["env"]["TRUNK_TEST_COLLECTION_ID"],
    }
    done = subprocess.run(
        [shutil.which("bash") or "bash", "-e", "-c", fetch["run"]],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert done.returncode != 0, done.stdout + done.stderr
    assert "list=" not in output.read_text(), output.read_text()


# ----- the root step and its verdict -----------------------------------------
def test_the_root_step_records_junit_and_defers_its_exit_code() -> None:
    step = _step("gate", step_id="root-playwright")
    assert step["continue-on-error"] is True
    assert step["env"]["PLAYWRIGHT_JUNIT_OUTPUT_FILE"] == "${{ runner.temp }}/junit-e2e-root.xml"
    assert step["env"]["PLAYWRIGHT_JUNIT_INCLUDE_PROJECT_IN_TEST_NAME"] == "1"
    run = step["run"]
    assert f'rm -f "${{RUNNER_TEMP:?RUNNER_TEMP must be set}}/{JUNIT_NAME}"' in run
    assert "set +e" in run
    assert 'echo "rc=${rc}" >> "$GITHUB_OUTPUT"' in run
    assert "pnpm test:e2e --output test-results/root-playwright --reporter=list,junit" in run
    assert 'exit "$rc"' in run


def test_the_verdict_step_follows_the_root_step_and_calls_the_script() -> None:
    steps = _jobs()["gate"]["steps"]
    index = next(i for i, step in enumerate(steps) if step.get("id") == "root-playwright")
    verdict = steps[index + 1]
    assert verdict["name"] == VERDICT_STEP
    assert verdict["if"] == "always()"
    assert "continue-on-error" not in verdict
    assert "working-directory" not in verdict
    assert verdict["env"]["TRUNK_QUARANTINE_LIST"] == "${{ needs.trunk-quarantine.outputs.list }}"
    assert verdict["env"]["ROOT_OUTCOME"] == "${{ steps.root-playwright.outcome }}"
    assert verdict["env"]["ROOT_RC"] == "${{ steps.root-playwright.outputs.rc }}"
    run = verdict["run"]
    assert ".venv/bin/python scripts/ci_trunk_quarantine_verdict.py" in run
    assert "--pytest-rc" in run and '"$ROOT_RC"' in run
    assert f'--junit "${{RUNNER_TEMP:?RUNNER_TEMP must be set}}/{JUNIT_NAME}"' in run
    assert "--shard e2e-root" in run
    assert "::error" in run


def test_the_gate_keeps_running_when_the_list_job_fails_or_is_skipped() -> None:
    gate = _jobs()["gate"]
    assert _needs(gate) == ["scope", "trunk-quarantine"]
    assert gate["if"] == (
        "${{ needs.scope.outputs.in_scope == 'true' && !cancelled() && needs.scope.result == 'success' }}"
    )


def test_the_token_never_appears_in_the_gate_or_any_checkout_job() -> None:
    text = E2E.read_text(encoding="utf-8")
    gate_source = text.split("\n  gate:\n", 1)[1].split("\n  extended:\n", 1)[0]
    assert "TRUNK_API_TOKEN" not in gate_source
    assert "TRUNK_API_TOKEN" not in yaml.safe_dump(_jobs()["gate"])
    list_job = _jobs()[LIST_JOB]
    assert list_job["runs-on"] == TOKEN_RUNS_ON_LINUX
    assert not _checks_out(list_job)
    for name, job in _jobs().items():
        if name == LIST_JOB:
            continue
        if _checks_out(job):
            assert "TRUNK_API_TOKEN" not in yaml.safe_dump(job), name


def test_the_upload_job_stages_in_runner_temp_and_is_not_a_verdict_need() -> None:
    verdict = _jobs()["verdict"]
    assert _needs(verdict) == ["scope", "gate"]
    assert UPLOAD_JOB not in _needs(verdict)
    assert LIST_JOB not in _needs(verdict)

    job = _jobs()[UPLOAD_JOB]
    assert job["runs-on"] == TOKEN_RUNS_ON_LINUX
    # The workspace persists on self-hosted runners; runner.temp is emptied per job.
    find = _step(UPLOAD_JOB, step_id="e2e-root-report")
    assert '"$RUNNER_TEMP/staged-e2e-root/junit-e2e-root.xml"' in find["run"], "Find reads the persistent workspace"
    assert _needs(job) == ["gate"]
    assert job["if"] == "always() && needs.gate.result != 'skipped'"
    assert not _checks_out(job)
    probe = _step(UPLOAD_JOB, step_id="trunk-token")
    assert probe["env"]["TRUNK_API_TOKEN"] == "${{ secrets.TRUNK_API_TOKEN }}"

    download = _step(UPLOAD_JOB, step_id="download-e2e-root")
    # No checkout in a token job, so the download is curl's SigV4, not the r2-artifact action.
    assert "uses" not in download and "--aws-sigv4" in download["run"]
    assert download["env"]["ARTIFACT_NAME"] == "e2e-trunk-junit-attempt-${{ github.run_attempt }}"
    assert download["env"]["DEST"] == "${{ runner.temp }}/staged-e2e-root"
    assert download.get("continue-on-error") is True
    assert "steps.trunk-token.outputs.present == 'true'" in download["if"]

    upload = _step(UPLOAD_JOB, step_id="trunk-flaky-tests-e2e-root")
    assert str(upload["uses"]).startswith(UPLOADER_SHA)
    assert upload.get("continue-on-error") is True
    assert upload["with"]["test-collection-id"] == COLLECTION
    assert upload["with"]["org-slug"] == "seat-a-org"
    assert upload["with"]["cli-version"] == "0.15.6"
    assert upload["with"]["use-uncloned-repo"] is True
    assert upload["with"]["quarantine"] is False
    assert upload["with"]["token"] == "${{ secrets.TRUNK_API_TOKEN }}"
    assert upload["with"]["junit-paths"] == "${{ runner.temp }}/staged-e2e-root/junit-e2e-root.xml"
    shard = next(step for step in _jobs(CI)["ci-insights"]["steps"] if step.get("id") == "trunk-flaky-tests-1")
    for key in (
        "gh-repo-url",
        "gh-repo-head-sha",
        "gh-repo-head-branch",
        "gh-repo-head-commit-epoch",
    ):
        assert upload["with"][key] == shard["with"][key], key

    staged = _step("gate", step_id="stage-e2e-junit")
    assert staged["if"] == "always()"
    assert staged.get("continue-on-error") is True
    assert JUNIT_NAME in staged["run"]
    artifact = next(
        step
        for step in _jobs()["gate"]["steps"]
        if step.get("uses") == R2_ACTION
        and step.get("with", {}).get("name", "").startswith("e2e-trunk-junit-attempt-")
    )
    assert artifact["with"]["mode"] == "upload"
    assert artifact["with"]["name"] == download["env"]["ARTIFACT_NAME"]
    assert artifact.get("continue-on-error") is True
    assert artifact["if"] == "always() && steps.stage-e2e-junit.outcome == 'success'"


@pytest.mark.parametrize(
    ("outcome", "rc", "quarantine", "want"),
    [
        ("success", "0", "", 0),
        ("failure", "1", _listed(TOAST), 0),
        ("failure", "1", _listed(OTHER), 1),
        ("failure", "1", _listed(), 1),
        ("cancelled", "", _listed(TOAST), 1),
    ],
)
def test_the_verdict_step_executes_as_written(
    tmp_path: Path, outcome: str, rc: str, quarantine: str, want: int
) -> None:
    done = _run_verdict_step(tmp_path, outcome, rc, quarantine)
    assert done.returncode == want, done.stdout + done.stderr
