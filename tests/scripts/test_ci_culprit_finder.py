"""The culprit finder names the first red commit of a red main window, running only the failing tests.

[if] the finder names a green commit, clears a red window or hides an error [then] fail, [else stop].

Regression lines:
  - if the second of three SHAs fails with stop on and the third still runs then the search wastes the budget
  - if a window with no failure reads anything but CULPRIT none then the checkpoint blames a green merge
  - if an errored SHA reads as pass or fail then a broken runner names a culprit
  - if tests missing at every SHA read CULPRIT none then a typo in a node id reads as a green window
  - if the culprit's PR is not the one whose merge commit is that SHA then the wrong PR is blamed
  - if the result JSON lacks sha, pr or tested then the checkpoint reader cannot parse it
  - if the summary's first line is not the request echo then the dispatcher cannot match its run
  - if the workflow uses GitHub artifact storage, leaves the main-fix route or can be cancelled then the guards are broken
  - if the fallback step cannot write CULPRIT error then a crashed search leaves the reader no verdict
  - if a node id missing at an older SHA hides a real failure of another node id then a later merge is blamed
  - if a collection or import error in the target test reads as a tool error then the culprit is never named
  - if a parametrized node id with spaces is split into pieces then that test is missing across the window
  - if a failed PR lookup drops the culprit sha from the verdict line then the found culprit is lost
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts import ci_culprit_finder as cf

pytestmark = pytest.mark.requirement("DEVOPS-8427")

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github" / "workflows" / "culprit-finder.yml"
A, B, C = "a" * 40, "b" * 40, "c" * 40


def _scripted(results: dict[str, str]) -> tuple[list[str], Callable[[str], str]]:
    ran: list[str] = []

    def run_at(sha: str) -> str:
        ran.append(sha)
        return results[sha]

    return ran, run_at


def _verdict(shas: list[str]) -> cf.Verdict:
    return cf.Verdict(request_id="r1", shas=shas, tests=["tests/x.py::t"])


# ----- verdict logic ---------------------------------------------------------------------------


def test_the_first_red_sha_is_the_culprit_and_the_search_stops_there() -> None:
    """[if] the second of three SHAs fails with stop on [then] it is named and the third never runs."""
    ran, run_at = _scripted({A: "pass", B: "fail", C: "fail"})
    verdict = cf.judge_window(_verdict([A, B, C]), run_at, stop_at_first_red=True)
    assert verdict.sha == B
    assert ran == [A, B]
    assert verdict.tested == [{"sha": A, "result": "pass"}, {"sha": B, "result": "fail"}]


def test_without_stop_every_sha_runs_and_the_first_red_is_still_named() -> None:
    """[if] stop_at_first_red is false [then] every SHA runs and the culprit is still the first red."""
    ran, run_at = _scripted({A: "pass", B: "fail", C: "fail"})
    verdict = cf.judge_window(_verdict([A, B, C]), run_at, stop_at_first_red=False)
    assert (verdict.sha, ran) == (B, [A, B, C])


def test_a_green_window_reads_culprit_none() -> None:
    """[if] no SHA fails [then] the verdict line is CULPRIT none."""
    _, run_at = _scripted({A: "pass", B: "missing"})
    verdict = cf.judge_window(_verdict([A, B]), run_at, stop_at_first_red=True)
    assert verdict.sha is None and verdict.line() == "CULPRIT none"


def test_an_errored_sha_ends_the_search_as_an_error() -> None:
    """[if] a SHA errors [then] the search raises rather than naming or clearing anything."""
    _, run_at = _scripted({A: "error", B: "fail"})
    with pytest.raises(cf.CulpritError, match="errored at"):
        cf.judge_window(_verdict([A, B]), run_at, stop_at_first_red=True)


def test_tests_missing_everywhere_is_an_error_not_a_green_window() -> None:
    """[if] the node ids exist at no SHA [then] the verdict is an error, never CULPRIT none."""
    _, run_at = _scripted({A: "missing", B: "missing"})
    with pytest.raises(cf.CulpritError, match="not found at any sha"):
        cf.judge_window(_verdict([A, B]), run_at, stop_at_first_red=True)


@pytest.mark.parametrize(
    ("rc", "result"),
    [(0, "pass"), (1, "fail"), (2, "fail"), (4, "missing"), (5, "missing"), (3, "error"), (137, "error")],
)
def test_pytest_exit_codes_map_to_results(rc: int, result: str) -> None:
    """[if] an interrupted or internal-error pytest reads as pass or fail [then] a broken runner names a culprit."""
    assert cf.classify_pytest_rc(rc) == result


def test_the_runner_creates_the_basetemp_parent(tmp_path: Path) -> None:
    """[if] the basetemp parent is missing when pytest starts [then] every test errors at setup and a green SHA reads red, [else stop]."""
    root = tmp_path / "missing" / "culprit-basetemp"
    cf.make_runner(["tests/x.py::t"], deadline=0.0, basetemp_root=root)
    assert root.is_dir()


# ----- inputs ----------------------------------------------------------------------------------


def test_inputs_are_validated_before_anything_is_checked_out() -> None:
    """[if] a non-sha, an empty test list, an option-shaped test or a bad request id gets through [then] fail."""
    assert cf.parse_shas(f" {A}  {B[:7]} ") == [A, B[:7]]
    for bad in ("", "main", "abc", "a" * 41, "HEAD~1"):
        with pytest.raises(cf.CulpritError):
            cf.parse_shas(bad)
    with pytest.raises(cf.CulpritError):
        cf.parse_tests("  ")
    with pytest.raises(cf.CulpritError):
        cf.parse_tests("tests/x.py::t --deselect=y")
    with pytest.raises(cf.CulpritError):
        cf.parse_request_id("r1; rm -rf /")
    assert cf.parse_request_id("") == "" and cf.parse_request_id("ckpt-41:2") == "ckpt-41:2"
    assert (cf.parse_bool("true"), cf.parse_bool("false")) == (True, False)
    with pytest.raises(cf.CulpritError):
        cf.parse_bool("yes")


def test_parametrized_node_ids_keep_their_spaces() -> None:
    """[if] whitespace inside a parametrize bracket splits a node id [then] that test is missing everywhere."""
    spaced = "tests/stems/test_x.py::test_flush[windows needs write access]"
    assert cf.parse_tests(f"{spaced}\ntests/a.py::t\n\n") == [spaced, "tests/a.py::t"]
    assert cf.parse_tests(f"tests/a.py::t {spaced}") == ["tests/a.py::t", spaced]
    assert cf.split_node_ids("  \n") == []


def test_one_failing_test_names_the_sha_whatever_the_others_did() -> None:
    """[if] a missing or passing node id outweighs a failing one at the same SHA [then] fail."""
    assert cf.combine_results({"a": "missing", "b": "fail"}) == "fail"
    assert cf.combine_results({"a": "pass", "b": "error"}) == "error"
    assert cf.combine_results({"a": "missing", "b": "pass"}) == "pass"
    assert cf.combine_results({"a": "missing"}) == "missing"


# ----- real git windows ------------------------------------------------------------------------

PASSING = "def test_a():\n    assert True\n"
FAILING = "def test_a():\n    assert False\n"
SPACED_ID = "tests/test_p.py::test_p[windows needs write access]"


def _parametrized(ok: bool) -> str:
    return (
        "import pytest\n\n"
        "@pytest.mark.parametrize('flag', [True], ids=['windows needs write access'])\n"
        f"def test_p(flag):\n    assert flag is {ok}\n"
    )


def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    cmd = ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args]
    return subprocess.run(cmd, cwd=repo, env=env, check=True, capture_output=True, text=True).stdout.strip()


def _window(repo: Path, commits: list[dict[str, str]]) -> list[str]:
    """A real repo with one commit per entry (path -> content), oldest first; returns the SHAs."""
    repo.mkdir()
    # Its own ini makes the repo pytest's rootdir, so no ancestor conftest or addopts leak in.
    (repo / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    shas: list[str] = []
    for files in commits:
        for rel, text in files.items():
            path = repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", f"commit {len(shas)}")
        shas.append(_git(repo, "rev-parse", "HEAD"))
    return shas


def _search(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shas: list[str], tests_raw: str) -> cf.Verdict:
    """Run the real runner over the real repo: real checkouts, a real pytest per node id."""
    monkeypatch.chdir(tmp_path / "repo")
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    tests = cf.parse_tests(tests_raw)
    runner = cf.make_runner(
        tests,
        deadline=time.monotonic() + 300,
        basetemp_root=tmp_path / "basetemp",
        provision=(sys.executable, "-c", "pass"),
        pytest=(sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"),
    )
    verdict = cf.Verdict(request_id="r1", shas=shas, tests=tests)
    return cf.judge_window(verdict, runner, stop_at_first_red=False)


def test_a_test_added_later_does_not_hide_the_real_culprit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] a node id that only exists at the newest SHA makes older SHAs read missing [then] the wrong merge is blamed."""
    shas = _window(
        tmp_path / "repo",
        [
            {"tests/test_old.py": PASSING},
            {"tests/test_old.py": FAILING},
            {"tests/test_new.py": PASSING},
        ],
    )
    raw = "tests/test_old.py::test_a\ntests/test_new.py::test_a\ntests/test_old.py::test_not_yet"
    verdict = _search(tmp_path, monkeypatch, shas, raw)
    assert [t["result"] for t in verdict.tested] == ["pass", "fail", "fail"]
    assert verdict.sha == shas[1]


def test_an_import_error_in_the_target_test_names_its_sha(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] a collection error (pytest exit 2) in the target test reads as a tool error [then] no culprit is named."""
    broken = "import module_that_does_not_exist\n\n" + PASSING
    shas = _window(tmp_path / "repo", [{"tests/test_old.py": PASSING}, {"tests/test_old.py": broken}])
    verdict = _search(tmp_path, monkeypatch, shas, "tests/test_old.py::test_a")
    assert [t["result"] for t in verdict.tested] == ["pass", "fail"]
    assert verdict.sha == shas[1] and verdict.error is None


def test_a_parametrized_id_with_spaces_runs_at_each_sha(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] a spaced parametrized id is split or missed [then] it reads missing and its culprit is never named."""
    shas = _window(
        tmp_path / "repo",
        [{"tests/test_p.py": _parametrized(True)}, {"tests/test_p.py": _parametrized(False)}],
    )
    verdict = _search(tmp_path, monkeypatch, shas, f"{SPACED_ID}\n")
    assert [t["result"] for t in verdict.tested] == ["pass", "fail"]
    assert verdict.sha == shas[1]


# ----- PR mapping ------------------------------------------------------------------------------


def test_the_pr_whose_merge_commit_is_the_sha_is_named() -> None:
    """[if] a commit appears in several PRs [then] the one that merged AS that commit is named."""
    pulls: list[dict[str, Any]] = [
        {"number": 10, "merged_at": "2026-11-03T10:00:00Z", "merge_commit_sha": C},
        {"number": 11, "merged_at": "2026-11-03T11:00:00Z", "merge_commit_sha": B},
        {"number": 12, "merged_at": None, "merge_commit_sha": B},
    ]
    assert cf.pick_pr(B, pulls) == 11
    assert cf.pick_pr(B[:9], pulls) == 11
    assert cf.pick_pr(A, pulls[:1]) == 10
    assert cf.pick_pr(A, []) is None
    assert cf.pick_pr(A, pulls[2:]) is None


# ----- outputs ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sha", "pr", "error", "line"),
    [
        (B, 42, None, f"CULPRIT {B} #42"),
        (B, None, None, f"CULPRIT {B} #none"),
        (None, None, None, "CULPRIT none"),
        (None, None, "boom", "CULPRIT error boom"),
    ],
)
def test_the_summary_line_and_json_follow_the_reader_contract(
    tmp_path: Path, sha: str | None, pr: int | None, error: str | None, line: str
) -> None:
    """[if] the line or JSON drifts from the contract [then] the checkpoint reader misparses it."""
    verdict = _verdict([A, B])
    verdict.sha, verdict.pr, verdict.error = sha, pr, error
    verdict.tested = [{"sha": A, "result": "pass"}]
    summary, out = tmp_path / "summary.md", tmp_path / "culprit-finder.json"
    cf.write_outputs(verdict, out, summary)
    lines = summary.read_text().splitlines()
    assert lines[0] == line
    assert lines[1].startswith("CULPRIT-JSON ")
    body = json.loads(out.read_text())
    assert json.loads(lines[1].removeprefix("CULPRIT-JSON ")) == body
    assert (body["sha"], body["pr"], body["tested"]) == (sha, pr, [{"sha": A, "result": "pass"}])


def test_a_failed_pr_lookup_keeps_the_culprit_sha(tmp_path: Path) -> None:
    """[if] a PR lookup failure after the search hides the culprit sha or skips the JSON [then] the verdict is lost."""
    verdict = _verdict([A, B])
    verdict.sha, verdict.pr_error = B, "pr lookup failed for b: HTTP 502"
    out = tmp_path / "culprit-finder.json"
    cf.write_outputs(verdict, out, None)
    assert verdict.line() == f"CULPRIT {B}"
    body = json.loads(out.read_text())
    assert (body["sha"], body["pr"], body["error"]) == (B, None, None)
    assert body["pr_error"].startswith("pr lookup failed")


def test_the_no_verdict_fallback_writes_culprit_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the fallback mode cannot write CULPRIT error and its JSON [then] a crashed search leaves no verdict."""
    summary, out = tmp_path / "summary.md", tmp_path / "culprit-finder.json"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    rc = cf.main(
        ["--no-verdict", "search step outcome=cancelled", "--shas", f"{A} {B}", "--request-id", "r1", "--out", str(out)]
    )
    assert rc == 1
    assert summary.read_text().splitlines()[0] == "CULPRIT error search step outcome=cancelled"
    assert json.loads(out.read_text())["sha"] is None


# ----- workflow --------------------------------------------------------------------------------


def _workflow() -> dict:
    assert WORKFLOW.is_file(), "culprit-finder workflow is missing"
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_the_workflow_is_dispatch_only_with_the_contract_inputs() -> None:
    """[if] the inputs, run-name or trigger drift [then] the checkpoint cannot dispatch or find the run."""
    doc = _workflow()
    on = doc[True]
    assert list(on) == ["workflow_dispatch"]
    inputs = on["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"shas", "tests", "stop_at_first_red", "request_id"}
    assert inputs["stop_at_first_red"]["type"] == "boolean" and inputs["stop_at_first_red"]["default"] is True
    assert doc["run-name"] == "culprit finder ${{ inputs.request_id }} ${{ inputs.shas }}"


def test_the_job_routing_timeout_and_concurrency_match_the_brief() -> None:
    """[if] the job leaves the main-fix route, loses its 30-minute cap or can be cancelled [then] fail."""
    doc = _workflow()
    job = doc["jobs"]["culprit"]
    assert job["name"] == "culprit finder"
    assert job["timeout-minutes"] == 30
    assert job["runs-on"] == "${{ fromJSON(vars.CI_RUNS_ON_MAIN_FIX || vars.CI_RUNS_ON_LINUX || '\"ubuntu-latest\"') }}"
    assert doc["concurrency"] == {"group": "culprit-finder-${{ github.run_id }}", "cancel-in-progress": False}


def test_the_request_echo_is_the_first_step_and_the_fallback_always_runs() -> None:
    """[if] anything writes to the summary before the request echo, or the fallback can be skipped [then] fail."""
    steps = _workflow()["jobs"]["culprit"]["steps"]
    assert steps[0]["name"] == "Echo the request"
    assert 'echo "request_id=${REQUEST_ID} shas=${SHAS}"' in steps[0]["run"]
    names = [s.get("name") for s in steps]
    fallback = steps[names.index("Write CULPRIT error when the search reached no verdict")]
    assert fallback["if"] == "always()" and "--no-verdict" in fallback["run"]
    search = steps[names.index("Run the failing tests at each SHA of the window")]
    assert "${{" not in search["run"], "inputs reach the shell through env, never by interpolation"


def test_older_shas_can_fetch_their_blobs_without_persisted_credentials() -> None:
    """[if] the partial clone's lazy fetch has no auth, or checkout persists the token [then] fail, [else stop]."""
    steps = _workflow()["jobs"]["culprit"]["steps"]
    checkout = next(s for s in steps if str(s.get("uses", "")).startswith("actions/checkout"))
    assert checkout["with"]["persist-credentials"] is False and checkout["with"]["fetch-depth"] == 0
    search = next(s for s in steps if s.get("id") == "search")
    assert "GIT_CONFIG_KEY_0=http.https://github.com/.extraheader" in search["run"]
    assert "::add-mask::" in search["run"] and search["env"]["GH_TOKEN"] == "${{ github.token }}"


def test_a_target_test_using_tmp_path_runs_at_every_sha(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] the per-SHA basetemp parent is never created [then] every tmp_path test errors and the window reads error."""
    uses_tmp = "def test_a(tmp_path):\n    (tmp_path / 'f').write_text('x')\n    assert {ok}\n"
    shas = _window(
        tmp_path / "repo",
        [{"tests/test_old.py": uses_tmp.format(ok="True")}, {"tests/test_old.py": uses_tmp.format(ok="False")}],
    )
    verdict = _search(tmp_path, monkeypatch, shas, "tests/test_old.py::test_a")
    assert [t["result"] for t in verdict.tested] == ["pass", "fail"]
    assert verdict.sha == shas[1] and verdict.error is None


def test_a_skipped_target_test_is_unavailable_not_green(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] a target test skipped on this runner reads as pass (pytest exits 0) [then] a red window reads CULPRIT none."""
    skipped = "import pytest\n\ndef test_a():\n    pytest.skip('no decoder build')\n"
    shas = _window(tmp_path / "repo", [{"tests/test_old.py": skipped}])
    with pytest.raises(cf.CulpritError, match="errored"):
        _search(tmp_path, monkeypatch, shas, "tests/test_old.py::test_a")


def test_a_partly_skipped_file_still_counts_its_executed_tests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[if] one skip in a file target hides the tests that did run [then] a usable file target always reads error."""
    mixed = "import pytest\n\ndef test_s():\n    pytest.skip('x')\n\n" + PASSING
    shas = _window(tmp_path / "repo", [{"tests/test_old.py": mixed}])
    verdict = _search(tmp_path, monkeypatch, shas, "tests/test_old.py")
    assert [t["result"] for t in verdict.tested] == ["pass"]
