"""The hourly selected run on main: the merge range it tests, and the culprits of a failure.

Why (main-core session, Thu 5 Nov 2026 18:15Z): three latent reds on main that day were each
introduced by a merge whose own main push run did not select the broken test (12042582bb broke
test_cmdk_index_capture and test_track_index_lazy_assets; #8534 broke test_ci_artifact_storage).
Each surfaced only when a later PR's selection happened to include the test, and that PR's owner
lost a cycle. `.github/workflows/main-hourly-selected.yml` runs once an hour on main's tip: the
selector (scripts/ci_test_selection.py) over the union of every file the hour's merges changed,
inside its own hard cap (ci/test-budget-main-hourly.yml), never the whole suite (the maintainer, Tue 27 Oct
2026: "Full-suite runs belong to scheduled nightly jobs ... never to merging").

Requirements (mini-PRD; statuses: ? todo, ✔︎ done, ✔︎ ✅ done + ran, ✔︎ ✅ 🎯 + regression tests):
  ✔︎ ✅ 🎯 R1 RANGE: base is the head of the previous successful run of this workflow when that head is a
      first-parent ancestor of the tip within MAX_COMMITS; otherwise the first-parent parent of the oldest main
      commit of the last `--since-minutes` (60). The tip itself as the previous head, or no commit in the window,
      is an EMPTY range (nothing runs, the run is green and says why).
      [if] the previous success is 3 commits back [then ⛔️ unless] base is that sha and 3 commits are listed
      [if] there is no previous success [then ⛔️ unless] base is the parent of the window's oldest commit
      [if] the previous success is not a first-parent ancestor [then ⛔️ unless] the window decides, and says so
  ✔︎ ✅ 🎯 R2 SELECTOR INPUT: range.json is a CI_MAIN_PUSH_RANGE record (`main_push`, `head`, `base`, `prs`,
      `declared`) so ci_test_selection selects over base..tip exactly as a main push does, plus the merged
      PRs' `CI-Tests:` lines; event.json is the push payload it reads.
      [if] a commit subject ends `(#123)` [then ⛔️ unless] 123 is in `prs`
      [if] the PR bodies cannot be read [then ⛔️ unless] the range stands and `errors` names why
  ✔︎ ✅ 🎯 R3 CULPRITS: on failure, each failing test module is attributed to the range's commits whose own
      files the selector maps to it (selected by that commit alone), the commits that edit the test file itself,
      and the commits the selector cannot narrow (a global path reaches every test, said so).
      [if] commit A imports-reaches the failing module and B does not [then ⛔️ unless] only A is a candidate
      [if] the failing test file was edited by C [then ⛔️ unless] C is a candidate
      [if] a commit changes a global path [then ⛔️ unless] it is a candidate marked `global`
  ✔︎ ✅ 🎯 R4 REPORT: the job summary names the range (base..tip), the selected module count and, on failure,
      the failing ids and the candidates; one `::error title=main-hourly-selected culprits::` annotation
      carries `range= candidates=<sha>#<pr>,... failing=<n>` for the fleet-af checkpoint owner to read.
      [if] pytest failed and the JUnit report names no failing test [then ⛔️ unless] the summary says UNKNOWN
  ✔︎ ✅ 🎯 R5 WALL CAP: the pytest step runs under a wall-time cap (`timeout --signal=INT`); a run the cap stops
      (`--wall-cap`) with no failing test id in its partial JUnit report is INCONCLUSIVE, not red: the summary says
      `result=inconclusive reason=wall-cap`, one `::warning title=main-hourly-selected inconclusive::` annotation
      says the same, and `report` exits 0 so a slow seat never paints main red. A failing id in the partial
      report is still a FAIL with the culprits annotation (a real failure is real however far the run got).
      [if] the cap stops a run whose partial report has no failure [then ⛔️ unless] rc 0, `result=inconclusive
      reason=wall-cap`, the inconclusive annotation and no `::error`
      [if] the cap stops a run whose partial report names a failing test [then ⛔️ unless] rc 1 and the culprits
      annotation names it

Usage:
    python -m scripts.ci_main_hourly range --repository OWNER/NAME --out-dir DIR [--since-minutes 60]
    python -m scripts.ci_main_hourly report --range DIR/range.json --selection DIR/selection/selection.json \\
        --junit REPORT.xml --pytest-rc N --out DIR/culprits.json [--wall-cap] [--ignore=tests/analysis ...]

`range` reads GH_TOKEN for the previous successful run and the merged PR bodies, writes DIR/range.json
and DIR/event.json, and prints `key=value` lines for GITHUB_OUTPUT. `report` exits with the pytest rc,
except 0 for a wall-cap stop with no failing id (R5) and 1 for one with a failing id.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from xml.etree import ElementTree

from scripts.ci_bundle_declarations import graphql_bodies
from scripts.ci_junit_failures import UNRESOLVED, failing_ids
from scripts.ci_main_push_range import MAIN_REF, pr_of
from scripts.ci_test_selection import parse_declarations
from scripts.ci_test_selection_rules import FullSuite, Policy, select_tests

REPO = Path(__file__).resolve().parent.parent
WORKFLOW = "main-hourly-selected.yml"
#: First-parent commits searched for the previous run's head: about ten hours of merges at 20 an hour.
MAX_COMMITS = 200
SINCE_MINUTES = 60
API = "https://api.github.com"
FETCH_TIMEOUT_S = 30
ANNOTATION_TITLE = "main-hourly-selected culprits"
#: R5: the annotation the fleet-af checkpoint owner reads as HOURLY-NONE, never RED.
INCONCLUSIVE_TITLE = "main-hourly-selected inconclusive"
#: Candidates per failing module shown in the summary; the JSON record keeps every one.
SUMMARY_CANDIDATES = 8


# ----- the range, pure ---------------------------------------------------------------


@dataclass(frozen=True)
class Commit:
    sha: str
    committed_epoch: int
    subject: str
    pr: int | None = None
    files: tuple[str, ...] = ()


@dataclass(frozen=True)
class Range:
    tip: str
    base: str | None
    commits: tuple[Commit, ...]
    source: str

    @property
    def empty(self) -> bool:
        return not self.commits


def choose_range(
    chain: list[Commit],
    parent_of_oldest: Callable[[str], str],
    previous_sha: str | None,
    previous_note: str,
    now_epoch: int,
    since_minutes: int,
) -> Range:
    """R1. `chain` is the tip's first-parent history, newest first, the tip included."""
    tip = chain[0].sha
    shas = [c.sha for c in chain]
    if previous_sha == tip:
        return Range(tip, tip, (), f"the previous successful run already tested this tip ({tip[:10]})")
    if previous_sha and previous_sha in shas:
        cut = shas.index(previous_sha)
        return Range(
            tip,
            previous_sha,
            tuple(chain[:cut]),
            f"since {previous_sha[:10]}, the head of the previous successful run of this workflow",
        )
    why = previous_note or (
        f"the previous successful run's head {previous_sha[:10]} is not among main's last {len(chain)} "
        "first-parent commits"
        if previous_sha
        else "no previous successful run of this workflow"
    )
    window = [c for c in chain if c.committed_epoch >= now_epoch - since_minutes * 60]
    if not window:
        return Range(tip, tip, (), f"{why}, and no main commit landed in the last {since_minutes} minutes")
    # The commit after the window's oldest in the chain is its first parent; past the chain's end, ask git.
    base = parent_of_oldest(window[-1].sha) if len(window) == len(chain) else chain[len(window)].sha
    return Range(tip, base, tuple(window), f"the last {since_minutes} minutes of main ({why})")


def selector_record(chosen: Range, declared: list[str], errors: list[str]) -> dict:
    """R2: a CI_MAIN_PUSH_RANGE record (scripts/ci_main_push_range.py) plus the hourly fields."""
    prs = list(dict.fromkeys(c.pr for c in chosen.commits if c.pr is not None))
    return {
        "main_push": True,
        "head": chosen.tip,
        "base": chosen.base,
        "scanned": len(chosen.commits),
        "prs": prs,
        "declared": declared,
        "errors": errors,
        "hourly": {"source": chosen.source, "empty": chosen.empty, "commits": [asdict(c) for c in chosen.commits]},
    }


# ----- culprits, pure ----------------------------------------------------------------


@dataclass
class Candidate:
    sha: str
    pr: int | None
    subject: str
    why: str


@dataclass
class Attribution:
    module: str
    failing: list[str]
    candidates: list[Candidate] = field(default_factory=list)


#: The modules a change set selects, or None when the selector cannot narrow it (a global path).
Selector = Callable[[list[str]], set[str] | None]


def attribute(failing: list[str], commits: list[Commit], select: Selector) -> list[Attribution]:
    """R3: the culprit candidates of each failing module, newest commit first."""
    by_module: dict[str, Attribution] = {}
    for test_id in failing:
        module = test_id.split("::", 1)[0]
        by_module.setdefault(module, Attribution(module, [])).failing.append(test_id)
    reach = {c.sha: select(list(c.files)) for c in commits}
    for attribution in by_module.values():
        for commit in commits:
            selected = reach[commit.sha]
            if attribution.module in commit.files:
                why = "edits the failing test file"
            elif selected is None:
                why = "global: the selector cannot narrow this commit, so it reaches every test"
            elif attribution.module in selected:
                why = "the selector maps this commit's files to the failing module"
            else:
                continue
            attribution.candidates.append(Candidate(commit.sha, commit.pr, commit.subject, why))
    return list(by_module.values())


def candidate_shas(attributions: list[Attribution]) -> list[tuple[str, int | None]]:
    """Every candidate once: direct ones (file or selector) before global ones, newest first within each."""
    direct = [(c.sha, c.pr) for a in attributions for c in a.candidates if not c.why.startswith("global")]
    wide = [(c.sha, c.pr) for a in attributions for c in a.candidates if c.why.startswith("global")]
    return list(dict.fromkeys(direct + wide))


def annotation_line(chosen_range: dict, attributions: list[Attribution], failing_count: int) -> str:
    """R4: the one annotation the fleet-af checkpoint owner parses (`candidates=` is comma separated)."""
    rendered = ",".join(f"{sha}#{pr}" if pr else sha for sha, pr in candidate_shas(attributions)) or "none"
    data = f"range={chosen_range['base']}..{chosen_range['head']} candidates={rendered} failing={failing_count}"
    return f"::error title={ANNOTATION_TITLE}::{data}"


def inconclusive_line(chosen_range: dict, reason: str, completed: int) -> str:
    """R5: the warning the fleet-af checkpoint owner reads as inconclusive (`result=` and `reason=` first)."""
    data = f"result=inconclusive reason={reason} range={chosen_range['base']}..{chosen_range['head']} completed={completed}"
    return f"::warning title={INCONCLUSIVE_TITLE}::{data}"


def summary_markdown(
    record: dict, selection: dict | None, outcome: str, failing: list[str], attributions: list[Attribution]
) -> str:
    hourly = record["hourly"]
    base, tip = record["base"] or "-", record["head"]
    lines = [
        f"### main-hourly-selected: {outcome}",
        "",
        "| field | value |",
        "|:--|:--|",
        f"| range | `{base[:10]}..{tip[:10]}` ({len(hourly['commits'])} commit(s)) |",
        f"| why this base | {hourly['source']} |",
    ]
    if selection is not None:
        budget = selection.get("budget") or {}
        lines += [
            f"| selected | {len(selection.get('selected_modules') or [])} test module(s), mode "
            f"`{selection.get('mode')}` |",
            f"| selector said | {selection.get('reason', '')[:300]} |",
        ]
        if budget:
            lines.append(
                f"| hard cap | {budget.get('planned_min')} of {budget.get('budget_min')} runner-minutes "
                f"planned, {len(budget.get('dropped') or [])} module(s) dropped |"
            )
    lines += ["", "Commits in the range:", ""]
    lines += [
        f"- `{c['sha'][:10]}` {('#' + str(c['pr']) + ' ') if c['pr'] else ''}{c['subject']}" for c in hourly["commits"]
    ] or ["- none"]
    if outcome.startswith("FAIL"):
        lines += ["", f"Failing tests ({len(failing)}):", ""] + [f"- `{t}`" for t in failing]
        lines += ["", "Culprit candidates (commits in the range the selector maps to each failing module):", ""]
        for a in attributions:
            lines.append(f"- `{a.module}`:")
            shown = a.candidates[:SUMMARY_CANDIDATES]
            lines += [
                f"  - `{c.sha[:10]}` {('#' + str(c.pr) + ' ') if c.pr else ''}{c.subject} ({c.why})" for c in shown
            ] or ["  - none in this range (an older merge, a flake, or a host problem)"]
            if len(a.candidates) > len(shown):
                lines.append(f"  - and {len(a.candidates) - len(shown)} more in culprits.json")
    return "\n".join(lines) + "\n"


# ----- effects: git, GitHub ------------------------------------------------------------


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, check=True).stdout


def first_parent_chain(limit: int = MAX_COMMITS) -> list[Commit]:
    out = _git("log", "--first-parent", f"--max-count={limit}", "--format=%H%x00%ct%x00%s", "HEAD")
    commits = []
    for line in out.splitlines():
        sha, epoch, subject = line.split("\x00", 2)
        commits.append(Commit(sha, int(epoch), subject, pr_of(subject)))
    return commits


def with_files(commit: Commit) -> Commit:
    """The files a first-parent commit changed against its first parent (a merge's whole PR)."""
    files = _git("diff", "--name-only", "--no-renames", f"{commit.sha}^1", commit.sha).split()
    return Commit(commit.sha, commit.committed_epoch, commit.subject, commit.pr, tuple(files))


def previous_success_head(repository: str, token: str) -> tuple[str | None, str]:
    """(head sha of the newest successful run of this workflow on main, note); a read failure is named, not hidden."""
    url = f"{API}/repos/{repository}/actions/workflows/{WORKFLOW}/runs?branch=main&status=success&per_page=1"
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_S) as response:
            runs = json.load(response)["workflow_runs"]
    except urllib.error.HTTPError as error:
        if error.code == 404:  # the workflow is not on main yet: no previous run, which is not a failure
            return None, ""
        return None, f"the previous successful run could not be read ({error!r})"
    except Exception as error:  # the window fallback runs, and the summary and a warning say why
        return None, f"the previous successful run could not be read ({error!r})"
    return (runs[0]["head_sha"] if runs else None), ""


def lane_selector(ignores: list[str]) -> Selector:
    """Attribution selects by name for workflow and CI-script changes (TEST-CUT item 3's rule), so a workflow
    merge maps to the tests that read that workflow instead of to every test; `ci/*` stays global."""
    policy = Policy(ci_config_by_name=True)

    def select(paths: list[str]) -> set[str] | None:
        if not paths:
            return set()
        try:
            return set(select_tests(paths, ignores, REPO, policy).modules)
        except FullSuite:
            return None

    return select


# ----- CLI -----------------------------------------------------------------------------


def _range(arguments: argparse.Namespace) -> int:
    token = os.environ.get("GH_TOKEN", "")
    previous, note = previous_success_head(arguments.repository, token) if token else (None, "GH_TOKEN is not set")
    if note:
        print(f"::warning title=main-hourly-selected range::{note}; the last {arguments.since_minutes} minutes decide")
    chain = first_parent_chain()
    chosen = choose_range(
        chain,
        lambda sha: _git("rev-parse", f"{sha}^1").strip(),
        previous,
        note,
        int(time.time()),
        arguments.since_minutes,
    )
    chosen = Range(chosen.tip, chosen.base, tuple(with_files(c) for c in chosen.commits), chosen.source)
    prs = list(dict.fromkeys(c.pr for c in chosen.commits if c.pr is not None))
    declared: list[str] = []
    errors: list[str] = []
    if prs:
        try:
            fetch_bodies = graphql_bodies(token)
            bodies = fetch_bodies(arguments.repository, prs) if token else {}
        except Exception as error:  # the range stands; only the optional declarations are lost, and said so
            bodies = {}
            errors.append(f"the merged PRs' bodies could not be read, so no `CI-Tests:` line joins: {error!r}")
        for pr in prs:
            paths, malformed = parse_declarations(bodies[pr].body if pr in bodies else "")
            declared += paths
            errors += [f"#{pr}: {problem}" for problem in malformed]
    record = selector_record(chosen, list(dict.fromkeys(declared)), errors)
    out = Path(arguments.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "range.json").write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
    (out / "event.json").write_text(json.dumps({"ref": MAIN_REF, "after": chosen.tip}) + "\n", encoding="utf-8")
    print(
        f"[main-hourly] {chosen.source}: {(chosen.base or '-')[:10]}..{chosen.tip[:10]}, "
        f"{len(chosen.commits)} commit(s), {len(prs)} PR(s), {len(declared)} declared module(s)",
        file=sys.stderr,
    )
    print(f"empty={'true' if chosen.empty else 'false'}")
    print(f"base={chosen.base or ''}")
    print(f"tip={chosen.tip}")
    print(f"commits={len(chosen.commits)}")
    return 0


def _report(arguments: argparse.Namespace) -> int:
    record = json.loads(Path(arguments.range).read_text(encoding="utf-8"))
    selection_path = Path(arguments.selection) if arguments.selection else None
    selection = (
        json.loads(selection_path.read_text(encoding="utf-8")) if selection_path and selection_path.is_file() else None
    )
    rc = arguments.pytest_rc
    failing: list[str] = []
    attributions: list[Attribution] = []
    junit = Path(arguments.junit) if arguments.junit else None
    inconclusive = False
    if record["hourly"]["empty"]:
        outcome = "EMPTY (nothing merged since the last tested tip)"
    elif rc == 0 and not arguments.wall_cap:
        outcome = "PASS"
    else:
        failing = failing_ids(junit, REPO) if junit and junit.is_file() else []
        resolved = [t for t in failing if not t.startswith(UNRESOLVED)]
        if arguments.wall_cap and not resolved:
            inconclusive = True
            outcome = (
                f"INCONCLUSIVE, result=inconclusive reason=wall-cap (pytest rc={rc}: the wall-time cap stopped the "
                f"run before the selection finished, and its {_completed(junit)} completed test(s) have no failure)"
            )
        elif not resolved:
            outcome = f"FAIL, failing tests UNKNOWN (pytest rc={rc}, the JUnit report names no failing test)"
        else:
            outcome = f"FAIL ({len(failing)} failing test id(s))"
            commits = [
                Commit(c["sha"], c["committed_epoch"], c["subject"], c["pr"], tuple(c["files"]))
                for c in record["hourly"]["commits"]
            ]
            attributions = attribute(resolved, commits, lane_selector(arguments.ignore))
    summary = summary_markdown(record, selection, outcome, failing, attributions)
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(summary)
    else:
        print(summary)
    Path(arguments.out).write_text(
        json.dumps(
            {
                "range": {"base": record["base"], "head": record["head"], "source": record["hourly"]["source"]},
                "outcome": outcome,
                "result": "inconclusive" if inconclusive else None,
                "reason": "wall-cap" if inconclusive else None,
                "pytest_rc": rc,
                "failing": failing,
                "attributions": [asdict(a) for a in attributions],
                "candidates": [{"sha": sha, "pr": pr} for sha, pr in candidate_shas(attributions)],
            },
            indent=1,
        )
        + "\n",
        encoding="utf-8",
    )
    if inconclusive:
        print(inconclusive_line(record, "wall-cap", _completed(junit)))
        return 0
    if record["hourly"]["empty"]:
        return rc
    if arguments.wall_cap:  # a failing id in the partial report: red with culprits, whatever the timeout's rc
        print(annotation_line(record, attributions, len(failing)))
        return 1
    if rc != 0:
        print(annotation_line(record, attributions, len(failing)))
    return rc


def _completed(junit: Path | None) -> int:
    """Named test cases the (possibly partial) JUnit report records; SIGINT leaves the interrupted test as a
    nameless `<testcase/>`, which is not a completed test. 0 when there is no report."""
    if not junit or not junit.is_file():
        return 0
    # Same trust as ci_junit_failures.failing_ids: this job's own pytest output, no external entities.
    # nosemgrep: python.lang.security.use-defused-xml-parse.use-defused-xml-parse
    return sum(1 for case in ElementTree.parse(junit).getroot().iter("testcase") if case.get("name"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    ranged = commands.add_parser("range", help="write range.json and event.json for this hour")
    ranged.add_argument("--repository", required=True)
    ranged.add_argument("--out-dir", required=True)
    ranged.add_argument("--since-minutes", type=int, default=SINCE_MINUTES)
    report = commands.add_parser("report", help="job summary, culprits.json and the culprits annotation")
    report.add_argument("--range", required=True)
    report.add_argument("--selection", default="")
    report.add_argument("--junit", default="")
    report.add_argument("--pytest-rc", type=int, required=True)
    report.add_argument("--out", required=True)
    report.add_argument("--wall-cap", action="store_true", help="R5: the pytest step's wall-time cap stopped the run")
    report.add_argument("--ignore", action="append", default=[], help="a lane --ignore path")
    arguments = parser.parse_args(argv)
    return _range(arguments) if arguments.command == "range" else _report(arguments)


if __name__ == "__main__":
    sys.exit(main())
