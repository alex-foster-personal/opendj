"""Name the merge that turned main red: run only the failing tests at each SHA of the red window.

Main's CI runs on the tip only, so a red verdict can cover up to ten merges (Tue 3 Nov 2026), and
the checkpoint's culprit-naming came back AMBIGUOUS or named the wrong PR. Running the full suite
on every commit costs too much (CI capacity means fewer tests, not more hosts). The standard answer
is a culprit finder: re-run ONLY the failing tests against each commit in the window, oldest first,
and stop at the first red one. .github/workflows/culprit-finder.yml drives this script.

The script is stdlib-only and is copied out of the checkout before it runs, because it checks out
other commits in the same workspace and those commits do not carry it.

Requirements (DEVOPS-8427, (private path)):
  ✔︎ ✅ 🎯 R1 each SHA is checked out and each given test runs there ON ITS OWN with the repo's pytest
      [if] a SHA is not 7-40 hex [then ⛔️] refuse before checking anything out
      [if] the tests list is empty [then ⛔️] refuse
      [if] pytest exits 0 / 1-2 / 4-5 [then ⛔️] that test records pass / fail / missing respectively
           (2 is a collection or import error in the target test, which is that test failing there)
      [if] the target test's module fails to import (pytest exits 4 for the node id, 2 for the
           file) [then ⛔️] that test records fail, never missing
      [if] a test's file does not exist at a SHA [then ⛔️] it records missing without running pytest,
           and the other tests still run (a missing node id must never hide a real failure)
      [if] a parametrized node id holds spaces [then ⛔️] it stays one node id (one per line, and
           whitespace inside [...] never splits)
  ✔︎ ✅ 🎯 R2 the first failing SHA is the culprit, and stop_at_first_red stops there
      [if] the second of three SHAs fails with stop on [then ⛔️] the third is never run
      [if] no SHA fails [then ⛔️] the verdict is CULPRIT none
      [if] a SHA errors (timeout, setup failure, other exit code) [then ⛔️] the verdict is CULPRIT error
      [if] the tests are missing at every SHA [then ⛔️] the verdict is CULPRIT error, never none
  ✔︎ ✅ 🎯 R3 the culprit maps to its PR, and the verdict is one summary line plus culprit-finder.json
      [if] the commit has a merged PR whose merge commit is the SHA [then ⛔️] that PR is named
      [if] the commit has no PR [then ⛔️] the line reads CULPRIT <sha> #none and pr is null
      [if] the PR lookup fails after a culprit was found [then ⛔️] the line still reads CULPRIT <sha>,
           pr is null and pr_error says why (the search verdict is not thrown away)
      [if] the JSON lacks sha, pr or tested [then ⛔️] the checkpoint reader cannot parse it

Regression lines live in tests/scripts/test_ci_culprit_finder.py.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class CFG:
    #: Pytest exit codes meaning the test fails at that commit (1 failed, 2 collection or import error).
    FAIL_RCS: tuple[int, ...] = (1, 2)
    #: Pytest exit codes meaning the node id does not exist at that commit (4 usage, 5 nothing collected).
    MISSING_RCS: tuple[int, ...] = (4, 5)
    #: Venv provisioning run at each SHA before its tests.
    PROVISION: tuple[str, ...] = ("scripts/ci_venv.sh", "3.11", "--lock", "pylock.ci.toml")
    PYTEST: tuple[str, ...] = (".venv/bin/pytest", "-p", "no:cacheprovider", "--timeout=600")
    RESULT_FILE: str = "culprit-finder.json"


_SHA = re.compile(r"^[0-9a-f]{7,40}$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{0,128}$")


class CulpritError(RuntimeError):
    """The window could not be judged; the verdict is CULPRIT error <reason>."""


@dataclass
class Verdict:
    request_id: str
    shas: list[str]
    tests: list[str]
    tested: list[dict[str, str]] = field(default_factory=list)
    sha: str | None = None
    pr: int | None = None
    error: str | None = None
    #: The PR lookup failed after the culprit was found; the line keeps the sha and drops the PR.
    pr_error: str | None = None

    def line(self) -> str:
        if self.error is not None:
            return f"CULPRIT error {self.error}"
        if self.sha is None:
            return "CULPRIT none"
        if self.pr_error is not None:
            return f"CULPRIT {self.sha}"
        if self.pr is None:
            return f"CULPRIT {self.sha} #none"
        return f"CULPRIT {self.sha} #{self.pr}"

    def as_json(self) -> dict:
        return {
            "request_id": self.request_id,
            "sha": self.sha,
            "pr": self.pr,
            "tested": self.tested,
            "tests": self.tests,
            "shas": self.shas,
            "error": self.error,
            "pr_error": self.pr_error,
        }


# ----- input parsing ---------------------------------------------------------------------------


def parse_shas(raw: str) -> list[str]:
    shas = raw.split()
    if not shas:
        raise CulpritError("no shas given")
    bad = [s for s in shas if not _SHA.match(s)]
    if bad:
        raise CulpritError(f"not a commit sha: {' '.join(bad)}")
    return shas


def split_node_ids(raw: str) -> list[str]:
    """One node id per line. Whitespace inside a parametrize bracket is part of the id; whitespace
    outside one still separates ids, so a space-joined list of plain ids keeps working."""
    ids: list[str] = []
    for line in raw.splitlines():
        current, depth = "", 0
        for ch in line:
            if ch == "[":
                depth += 1
            elif ch == "]" and depth:
                depth -= 1
            if ch.isspace() and depth == 0:
                if current:
                    ids.append(current)
                current = ""
                continue
            current += ch
        if current.strip():
            ids.append(current.strip())
    return ids


def parse_tests(raw: str) -> list[str]:
    tests = split_node_ids(raw)
    if not tests:
        raise CulpritError("no tests given")
    bad = [t for t in tests if t.startswith("-")]
    if bad:
        raise CulpritError(f"tests must be pytest node ids, not options: {' '.join(bad)}")
    return tests


def parse_request_id(raw: str) -> str:
    if not _REQUEST_ID.match(raw):
        raise CulpritError("request_id may only hold letters, digits and . _ : -")
    return raw


def parse_bool(raw: str) -> bool:
    if raw in ("true", "false"):
        return raw == "true"
    raise CulpritError(f"stop_at_first_red must be true or false, got {raw!r}")


# ----- verdict logic ---------------------------------------------------------------------------


def classify_pytest_rc(rc: int) -> str:
    if rc == 0:
        return "pass"
    if rc in CFG.FAIL_RCS:
        return "fail"
    if rc in CFG.MISSING_RCS:
        return "missing"
    return "error"


def combine_results(by_test: dict[str, str]) -> str:
    """One SHA's result from its per-test results: any fail names it, then any error, then any pass."""
    for result in ("fail", "error", "pass"):
        if result in by_test.values():
            return result
    return "missing"


def ran_nothing(junit_xml: Path) -> bool:
    """True when pytest exited 0 without executing a test: every collected case was skipped.

    A skip (no decoder build, wrong platform) is an unavailable capability, not a pass, so
    it must not clear a red window with `CULPRIT none` (#8427 review)."""
    # The report is the one this run's own pytest just wrote to a private temp dir, and the script must
    # stay stdlib-only (it is copied out before other SHAs are checked out), so defusedxml is not an option.
    tree = ET.parse(junit_xml)  # nosemgrep: python.lang.security.use-defused-xml-parse.use-defused-xml-parse
    cases = tree.getroot().iter("testcase")
    return all(case.find("skipped") is not None for case in cases)


def node_file(node_id: str) -> str:
    return node_id.split("::", 1)[0]


def judge_window(
    verdict: Verdict,
    run_at: Callable[[str], str],
    stop_at_first_red: bool,
) -> Verdict:
    """Run each SHA oldest first; the first `fail` is the culprit, the first `error` ends the search."""
    for sha in verdict.shas:
        result = run_at(sha)
        verdict.tested.append({"sha": sha, "result": result})
        if result == "error":
            raise CulpritError(f"tests errored at {sha}, the window cannot be judged")
        if result == "fail" and verdict.sha is None:
            verdict.sha = sha
            if stop_at_first_red:
                break
    if all(t["result"] == "missing" for t in verdict.tested):
        raise CulpritError("the tests were not found at any sha, check the node ids")
    return verdict


def pick_pr(sha: str, pulls: list[dict]) -> int | None:
    """The PR whose merge commit IS this SHA, else the first merged PR carrying it, else none."""
    merged = [p for p in pulls if p.get("merged_at")]
    for pull in merged:
        if pull.get("merge_commit_sha") == sha or str(pull.get("merge_commit_sha", "")).startswith(sha):
            return int(pull["number"])
    return int(merged[0]["number"]) if merged else None


# ----- effects ---------------------------------------------------------------------------------


def _run(cmd: list[str], timeout_s: float) -> int:
    print(f"[culprit] $ {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, timeout=max(timeout_s, 1), check=False).returncode


def make_runner(
    tests: list[str],
    deadline: float,
    basetemp_root: Path,
    provision: Sequence[str] = CFG.PROVISION,
    pytest: Sequence[str] = CFG.PYTEST,
) -> Callable[[str], str]:
    # pytest creates --basetemp itself but not its parents; a missing parent errors every test at
    # setup with rc 1, which reads as a red SHA (the false culprit of run 41675331219).
    basetemp_root.mkdir(parents=True, exist_ok=True)

    def run_at(sha: str) -> str:
        left = deadline - time.monotonic()
        if left <= 0:
            raise CulpritError(f"budget spent before {sha}")
        by_test: dict[str, str] = {}
        try:
            if _run(["git", "checkout", "--detach", "--quiet", sha], left) != 0:
                raise CulpritError(f"git checkout {sha} failed")
            if _run(list(provision), deadline - time.monotonic()) != 0:
                raise CulpritError(f"venv provisioning failed at {sha}")
            # One pytest per node id: a node id missing at this SHA exits 4 and, passed together
            # with the others, would hide a real failure among them (#8427 review).
            for index, node_id in enumerate(tests):
                if not Path(node_file(node_id)).exists():
                    by_test[node_id] = "missing"
                    continue
                basetemp = basetemp_root / sha / str(index)
                basetemp.parent.mkdir(parents=True, exist_ok=True)  # pytest mkdirs basetemp without parents
                report = basetemp.parent / f"{index}.junit.xml"
                rc = _run(
                    [*pytest, f"--basetemp={basetemp}", f"--junitxml={report}", node_id], deadline - time.monotonic()
                )
                if rc in CFG.MISSING_RCS and "::" in node_id:
                    # A module that fails to import exits 4 ("found no collectors") for a node id
                    # inside it; collecting the file alone tells that apart from an id not defined yet.
                    probe = [*pytest, f"--basetemp={basetemp}-collect", "--collect-only", "-q", node_file(node_id)]
                    if _run(probe, deadline - time.monotonic()) in CFG.FAIL_RCS:
                        rc = 2
                by_test[node_id] = classify_pytest_rc(rc)
                if by_test[node_id] == "pass" and ran_nothing(report):
                    by_test[node_id] = "error"
                    print(f"[culprit] {sha} {node_id} was skipped, not run: unavailable here", flush=True)
                print(f"[culprit] {sha} {node_id} -> {by_test[node_id]} (pytest rc {rc})", flush=True)
        except subprocess.TimeoutExpired as exc:
            raise CulpritError(f"budget spent while testing {sha}") from exc
        result = combine_results(by_test)
        print(f"[culprit] {sha} -> {result}", flush=True)
        return result

    return run_at


def fetch_pulls(repo: str, sha: str) -> list[dict]:
    out = subprocess.run(
        ["gh", "api", f"repos/{repo}/commits/{sha}/pulls"], capture_output=True, text=True, check=False
    )
    if out.returncode != 0:
        raise CulpritError(f"pr lookup failed for {sha}: {out.stderr.strip()[:200]}")
    return json.loads(out.stdout)


def write_outputs(verdict: Verdict, out_path: Path, summary_path: Path | None) -> None:
    body = json.dumps(verdict.as_json(), separators=(",", ":"))
    out_path.write_text(body + "\n", encoding="utf-8")
    lines = [verdict.line(), f"CULPRIT-JSON {body}", "", "| sha | result |", "|:--|:--|"]
    lines += [f"| `{t['sha']}` | {t['result']} |" for t in verdict.tested]
    text = "\n".join(lines) + "\n"
    print(text, flush=True)
    if summary_path is not None:
        with summary_path.open("a", encoding="utf-8") as fh:
            fh.write(text)


# ----- entry point -----------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--shas", required=True)
    parser.add_argument("--request-id", default="")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--no-verdict", metavar="REASON", help="only record CULPRIT error REASON (the workflow's fallback)"
    )
    parser.add_argument("--tests", default="")
    parser.add_argument("--stop-at-first-red", default="")
    parser.add_argument("--repo", default="")
    parser.add_argument("--budget-s", type=int, default=0)
    parser.add_argument("--restore-sha", default="")
    args = parser.parse_args(argv)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    summary_path = Path(summary) if summary else None
    verdict = Verdict(request_id=args.request_id, shas=args.shas.split(), tests=split_node_ids(args.tests))
    if args.no_verdict is not None:
        verdict.error = args.no_verdict
        write_outputs(verdict, args.out, summary_path)
        return 1
    if not (args.repo and args.budget_s > 0 and args.restore_sha):
        parser.error("a search needs --repo, a positive --budget-s and --restore-sha")
    deadline = time.monotonic() + args.budget_s
    try:
        verdict.request_id = parse_request_id(args.request_id)
        verdict.shas = parse_shas(args.shas)
        verdict.tests = parse_tests(args.tests)
        stop = parse_bool(args.stop_at_first_red)
        runner = make_runner(verdict.tests, deadline, args.out.parent / "culprit-basetemp")
        judge_window(verdict, runner, stop)
    except CulpritError as exc:
        verdict.error = str(exc)
    except Exception as exc:
        # Record the crash and what was tested so far, then fail loud with the traceback.
        verdict.error = f"crash {type(exc).__name__}: {exc}"
        write_outputs(verdict, args.out, summary_path)
        raise
    finally:
        subprocess.run(["git", "checkout", "--detach", "--quiet", args.restore_sha], check=False)
    if verdict.error is None and verdict.sha is not None:
        try:
            verdict.pr = pick_pr(verdict.sha, fetch_pulls(args.repo, verdict.sha))
        except CulpritError as exc:
            verdict.pr_error = str(exc)
    write_outputs(verdict, args.out, summary_path)
    return 1 if verdict.error is not None else 0


if __name__ == "__main__":
    sys.exit(main())
