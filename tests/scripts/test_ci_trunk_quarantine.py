"""Trunk flaky-test quarantine is armed in the fast lane, and fails closed.

ADR-NEW-trunk-flaky-quarantine-on (the maintainer, Wed 30 Sep 2026): "Set TRUNK
QUARANTINE to True so flakey tests stop blocking merges so aggressively."

Regression lines:
  - if an unlisted failure passes then quarantine is hiding a real regression
  - if a failure whose every failed test is listed still fails then quarantine does nothing
  - if exit 2, 4, 124 or 137 passes with listed failures then a collection error, a floor
    gate or a timeout is being excused as a flake
  - if an unreachable Trunk API yields a usable list then an outage quarantines by accident
  - if an empty, not-ok or oversized list excuses anything then the fail-closed contract is gone
  - if exit 1 with no failed testcase passes then an unexplained red reads green
  - if the pytest step loses continue-on-error then quarantine can never pass a shard
  - if the verdict step loses always() or gains continue-on-error then a red shard reads green
  - if the list job checks out code or leaves the hosted runner then the org token meets
    repository code on a shared self-hosted runner
  - if the test job's `if` loses !cancelled() or the scope result then a failed list job skips
    every required shard, or a failed scope runs them
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"
VERDICT_SCRIPT = REPO_ROOT / "scripts" / "ci_trunk_quarantine_verdict.py"
LIST_JOB = "trunk-quarantine-list"
VERDICT_STEP = "Fast lane verdict (pytest exit code, Trunk quarantine applied)"

FLAKY = ("tests.webui.test_queue", "test_flaky_one")
REAL = ("tests.webui.test_queue", "test_real_regression")


# -----------------------------------------------------------------------------
def _jobs() -> dict:
    return yaml.safe_load(CI.read_text())["jobs"]


def _step(job: str, *, step_id: str | None = None, name: str | None = None) -> dict:
    steps = [
        s
        for s in _jobs()[job]["steps"]
        if (step_id and s.get("id") == step_id) or (name and s.get("name") == name)
    ]
    assert len(steps) == 1, f"{job}: want one step id={step_id} name={name}, got {len(steps)}"
    return steps[0]


def _junit(tmp_path: Path, cases: list[tuple[str, str, str]]) -> Path:
    body = []
    for classname, name, kind in cases:
        inner = "" if kind == "pass" else f'<{kind} message="boom">trace</{kind}>'
        body.append(
            f'<testcase classname="{classname}" name="{name}" time="0.1">{inner}</testcase>'
        )
    path = tmp_path / "junit-shard-1.xml"
    path.write_text(
        f'<?xml version="1.0"?><testsuites><testsuite name="pytest">{"".join(body)}</testsuite></testsuites>'
    )
    return path


def _listed(*keys: tuple[str, str], status: str = "ok") -> str:
    tests = ",".join(f'{{"c": "{c}", "n": "{n}"}}' for c, n in keys)
    return f'{{"status": "{status}", "tests": [{tests}]}}'


def _verdict(rc: str, quarantine: str, junit: Path) -> tuple[int, str]:
    done = subprocess.run(
        [
            sys.executable,
            str(VERDICT_SCRIPT),
            "--pytest-rc",
            rc,
            "--junit",
            str(junit),
            "--shard",
            "1",
        ],
        env={"PATH": os.environ.get("PATH", ""), "TRUNK_QUARANTINE_LIST": quarantine},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return done.returncode, done.stdout + done.stderr


# ----- the verdict script ----------------------------------------------------
def test_an_unlisted_failure_fails_the_shard(tmp_path: Path) -> None:
    junit = _junit(
        tmp_path, [(*FLAKY, "failure"), (*REAL, "failure"), ("tests.x", "test_ok", "pass")]
    )
    rc, out = _verdict("1", _listed(FLAKY), junit)
    assert rc == 1 and "test_real_regression" in out and "VERDICT: FAIL" in out, out
    rc, out = _verdict("1", _listed(), junit)
    assert rc == 1, "empty list must excuse nothing: " + out


def test_a_listed_only_failure_passes_the_shard(tmp_path: Path) -> None:
    junit = _junit(
        tmp_path, [(*FLAKY, "failure"), (*REAL, "error"), ("tests.x", "test_ok", "pass")]
    )
    rc, out = _verdict("1", _listed(FLAKY, REAL), junit)
    assert (
        rc == 0 and "VERDICT: PASS" in out and "::warning title=Trunk quarantine excused 2" in out
    ), out


def test_exit_zero_passes_without_any_list(tmp_path: Path) -> None:
    rc, out = _verdict("0", "", tmp_path / "absent.xml")
    assert rc == 0, out


@pytest.mark.parametrize("pytest_rc", ["2", "3", "4", "5", "124", "137", ""])
def test_any_exit_but_one_fails_even_when_every_failure_is_listed(
    tmp_path: Path, pytest_rc: str
) -> None:
    junit = _junit(tmp_path, [(*FLAKY, "failure")])
    rc, out = _verdict(pytest_rc, _listed(FLAKY), junit)
    assert rc == 1 and "only exit 1 can be excused" in out, out
    assert _verdict("1", _listed(FLAKY), junit)[0] == 0, "control: the same report at exit 1 passes"


@pytest.mark.parametrize(
    "quarantine",
    [
        "",
        "not json",
        '{"status": "unreachable"}',
        '{"status": "ok"}',
        '["x"]',
        '{"status": "ok", "tests": [{"c": ""}]}',
    ],
)
def test_an_unusable_list_fails_closed(tmp_path: Path, quarantine: str) -> None:
    junit = _junit(tmp_path, [(*FLAKY, "failure")])
    rc, out = _verdict("1", quarantine, junit)
    assert rc == 1 and "nothing is quarantined" in out, out


def test_an_oversized_list_is_a_mass_false_flag(tmp_path: Path) -> None:
    junit = _junit(tmp_path, [(*FLAKY, "failure")])
    padding = [("tests.pad", f"test_{i}") for i in range(400)]
    rc, out = _verdict("1", _listed(FLAKY, *padding), junit)
    assert rc == 1 and "mass false flag" in out, out
    assert _verdict("1", _listed(FLAKY, *padding[:100]), junit)[0] == 0, (
        "control: a plausible list passes"
    )


def test_too_many_excused_failures_in_one_shard_is_an_infra_burst(tmp_path: Path) -> None:
    keys = [("tests.burst", f"test_{i}") for i in range(11)]
    junit = _junit(tmp_path, [(*k, "error") for k in keys])
    rc, out = _verdict("1", _listed(*keys), junit)
    assert rc == 1 and "infrastructure burst" in out, out
    junit = _junit(tmp_path, [(*k, "error") for k in keys[:10]])
    assert _verdict("1", _listed(*keys), junit)[0] == 0, "control: ten excused failures pass"


def test_exit_one_without_a_usable_report_fails(tmp_path: Path) -> None:
    rc, out = _verdict("1", _listed(FLAKY), tmp_path / "absent.xml")
    assert rc == 1 and "does not exist" in out, out
    (tmp_path / "bad.xml").write_text("<testsuites><testsuite>")
    assert _verdict("1", _listed(FLAKY), tmp_path / "bad.xml")[0] == 1
    rc, out = _verdict("1", _listed(FLAKY), _junit(tmp_path, [(*FLAKY, "pass")]))
    assert rc == 1 and "no failed testcase" in out, out


# ----- the list job, executed against an unreachable API ---------------------
def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_an_unreachable_list_fails_closed_end_to_end(tmp_path: Path) -> None:
    fetch = _step(LIST_JOB, step_id="fetch")
    bash, curl, jq = shutil.which("bash"), shutil.which("curl"), shutil.which("jq")
    if not (bash and curl and jq):
        pytest.fail("bash, curl and jq are required to execute the list job's own run block")
    output = tmp_path / "github_output"
    output.write_text("")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "GITHUB_OUTPUT": str(output),
        "TRUNK_API_TOKEN": "not-a-real-token",
        "TRUNK_API_URL": f"http://127.0.0.1:{_closed_port()}",
        "TRUNK_TEST_COLLECTION_ID": fetch["env"]["TRUNK_TEST_COLLECTION_ID"],
    }
    done = subprocess.run(
        [bash, "-e", "-c", fetch["run"]],
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert done.returncode != 0, done.stdout + done.stderr
    assert "list=" not in output.read_text(), output.read_text()
    junit = _junit(tmp_path, [(*FLAKY, "failure")])
    rc, out = _verdict("1", "", junit)
    assert rc == 1 and "nothing is quarantined" in out, out


def test_the_list_job_fails_without_a_token(tmp_path: Path) -> None:
    fetch = _step(LIST_JOB, step_id="fetch")
    output = tmp_path / "github_output"
    output.write_text("")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "GITHUB_OUTPUT": str(output),
        "TRUNK_API_URL": "http://127.0.0.1:9",
    }
    done = subprocess.run(
        [shutil.which("bash") or "bash", "-e", "-c", fetch["run"]],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode != 0 and "not provisioned" in done.stderr, done.stderr
    assert output.read_text() == ""


# ----- the wiring --------------------------------------------------------------
def test_the_list_job_runs_no_repository_code_on_a_hosted_runner() -> None:
    job = _jobs()[LIST_JOB]
    assert job["runs-on"] == "ubuntu-latest", job["runs-on"]
    assert all("uses" not in s for s in job["steps"]), (
        "the list job must not run any action, checkout included"
    )
    assert _step(LIST_JOB, step_id="fetch")["continue-on-error"] is True
    assert job["outputs"]["list"] == "${{ steps.fetch.outputs.list }}"
    assert "vars.TRUNK_QUARANTINE_CI == 'true'" in job["if"], job["if"]


def test_the_shards_need_the_list_but_survive_its_failure() -> None:
    test = _jobs()["test"]
    assert test["needs"] == ["scope", LIST_JOB], test["needs"]
    for clause in (
        "!cancelled()",
        "needs.scope.result == 'success'",
        "needs.scope.outputs.in_scope == 'true'",
    ):
        assert clause in test["if"], (clause, test["if"])
    assert "always()" not in test["if"], "always() would run the shards on a cancelled run"


def test_the_pytest_step_records_its_exit_code_and_defers_the_verdict() -> None:
    pytest_step = _step("test", step_id="pytest-shard")
    assert pytest_step["continue-on-error"] is True
    assert 'echo "rc=${rc}" >> "$GITHUB_OUTPUT"' in pytest_step["run"]


def test_the_verdict_step_is_last_unconditional_and_cannot_be_swallowed() -> None:
    steps = _jobs()["test"]["steps"]
    verdict = steps[-1]
    assert verdict["name"] == VERDICT_STEP, "the verdict must be the job's last step"
    assert verdict["if"] == "always()"
    assert "continue-on-error" not in verdict
    assert verdict["env"]["PYTEST_OUTCOME"] == "${{ steps.pytest-shard.outcome }}"
    assert verdict["env"]["PYTEST_RC"] == "${{ steps.pytest-shard.outputs.rc }}"
    assert verdict["env"]["TRUNK_QUARANTINE_LIST"] == f"${{{{ needs.{LIST_JOB}.outputs.list }}}}"


def _run_verdict_step(tmp_path: Path, outcome: str, rc: str, quarantine: str) -> int:
    step = _step("test", name=VERDICT_STEP)
    runner_temp = tmp_path / "runner_temp"
    runner_temp.mkdir(exist_ok=True)
    _junit(runner_temp, [(*FLAKY, "failure")])
    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True, exist_ok=True)
    if not venv_python.exists():
        venv_python.symlink_to(sys.executable)
    (tmp_path / "scripts").mkdir(exist_ok=True)
    shutil.copy(VERDICT_SCRIPT, tmp_path / "scripts" / VERDICT_SCRIPT.name)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "RUNNER_TEMP": str(runner_temp),
        "PYTEST_OUTCOME": outcome,
        "PYTEST_RC": rc,
        "TRUNK_QUARANTINE_LIST": quarantine,
        "SHARD": "1",
    }
    return subprocess.run(
        [shutil.which("bash") or "bash", "-e", "-c", step["run"]],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        timeout=60,
        check=False,
    ).returncode


@pytest.mark.parametrize(
    ("outcome", "rc", "quarantine", "want"),
    [
        ("success", "0", "", 0),
        ("failure", "1", _listed(FLAKY), 0),
        ("failure", "1", _listed(), 1),
        ("failure", "1", "", 1),
        ("failure", "124", _listed(FLAKY), 1),
        ("failure", "", _listed(FLAKY), 1),
        ("skipped", "", _listed(FLAKY), 1),
        ("cancelled", "", _listed(FLAKY), 1),
    ],
)
def test_the_verdict_step_executes_as_written(
    tmp_path: Path, outcome: str, rc: str, quarantine: str, want: int
) -> None:
    assert _run_verdict_step(tmp_path, outcome, rc, quarantine) == want
