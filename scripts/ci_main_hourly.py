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
      carries `range= candidates=<sha>#<pr>,... failing=<n> ids=<id>,...` for the fleet-af checkpoint owner to
      read; `ids=` lists the first ANNOTATION_IDS failing ids and `ids_omitted=<k>` counts the rest.
      [if] pytest failed and the JUnit report names no failing test [then ⛔️ unless] the summary says UNKNOWN
      [if] 4 tests fail [then ⛔️ unless] the annotation carries `failing=4 ids=` and all 4 ids
      [if] 12 tests fail [then ⛔️ unless] it names 10 of them and `ids_omitted=2`
  ✔︎ ✅ 🎯 R5 WALL CAP: the pytest step runs under a wall-time cap (`timeout --signal=INT`); a run the cap stops
      (`--wall-cap`) with no failing test id in its partial JUnit report is INCONCLUSIVE, not red: the summary says
      `result=inconclusive reason=wall-cap`, one `::warning title=main-hourly-selected inconclusive::` annotation
      says the same, and `report` exits 0 so a slow seat never paints main red. A failing id in the partial
      report is still a FAIL with the culprits annotation (a real failure is real however far the run got).
      Exact cap semantics (Fri 6 Nov 2026, run 42138780674): a test with NO JUnit record (pytest was stopped
      before writing it: the report is absent, truncated, or holds only SIGINT's nameless `<testcase/>`) is
      inconclusive and never failing; every RECORDED failure or error is a real failing id, pytest-timeout ones
      included (that run's four `Timeout (>300.0s)` setup errors were pytest's own verdicts, raised before
      the cap). A recorded failure the checkout cannot map to a file is FAIL UNKNOWN, never inconclusive.
      [if] the cap stops a run whose partial report has no failure [then ⛔️ unless] rc 0, `result=inconclusive
      reason=wall-cap`, the inconclusive annotation and no `::error`
      [if] the cap stops a run whose partial report names a failing test [then ⛔️ unless] rc 1 and the culprits
      annotation names it
      [if] the cap stops a run whose report records a pytest-timeout error [then ⛔️ unless] rc 1 and it is listed
      [if] the cap stops a run whose report is truncated XML [then ⛔️ unless] rc 0 and inconclusive
  ✔︎ ✅ 🎯 R6 BOUNDED REPORT: `report` finishes well inside its 5-minute step on a range of MAX_COMMITS commits and a
      JUnit report of thousands of cases. Run 42188076608 (Fri 6 Nov 2026 08:39Z, 27 commits) timed out with no
      output at all: attribution called the selector once per commit and each call reloaded the import graph and
      re-parsed every source (165 s on an idle Mac, past 300 s on a loaded seat). Now the graph loads once, pure
      source reads are cached, one call serves each distinct file set, the JUnit report is stream-parsed, and the
      range and selection are logged and summarized before attribution starts. Past ATTRIBUTION_BUDGET_S the older
      commits are not selected: a `::warning title=main-hourly-selected cut::` line names them, the culprits
      annotation adds `unattributed=<k>` after its other keys, and an edit of a failing test file still names its
      commit.
      [if] the budget runs out [then ⛔️ unless] the culprits annotation still prints, with `unattributed=` and a cut line
      [if] 200 commits share a few file sets [then ⛔️ unless] the selector runs once per file set
      [if] the JUnit report holds 3000 cases [then ⛔️ unless] the failing ids are exactly its failures

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
from scripts.ci_junit_failures import UNRESOLVED, node_id
from scripts.ci_main_push_range import MAIN_REF, pr_of
from scripts.ci_test_selection import parse_declarations
from scripts.ci_test_selection_rules import FullSuite, Graph, Policy, select_tests

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
#: Failing ids the culprits annotation lists; the summary and culprits.json keep every one.
ANNOTATION_IDS = 10
#: Candidates per failing module shown in the summary; the JSON record keeps every one.
SUMMARY_CANDIDATES = 8
#: R6: seconds `report` spends on per-commit selector attribution before it stops and names the rest as cut.
#: The workflow step times out at 5 minutes (run 42188076608 spent all 5 on 27 selector calls and printed nothing).
ATTRIBUTION_BUDGET_S = 150
#: R6: the warning naming the commits the budget left unattributed.
CUT_TITLE = "main-hourly-selected cut"


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
    return attribute_within(failing, commits, select, None)[0]


def attribute_within(
    failing: list[str],
    commits: list[Commit],
    select: Selector,
    budget_s: float | None,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[list[Attribution], list[Commit]]:
    """R3 + R6: (attributions, the commits the budget left unattributed, oldest last). Commits are selected newest
    first, one selector call per distinct file set, until `budget_s` is spent; an edit of the failing test file is
    a candidate in every commit, cut or not, since it needs no selector."""
    by_module: dict[str, Attribution] = {}
    for test_id in failing:
        module = test_id.split("::", 1)[0]
        by_module.setdefault(module, Attribution(module, [])).failing.append(test_id)
    started = clock()
    by_files: dict[tuple[str, ...], set[str] | None] = {}
    reach: dict[str, set[str] | None] = {}
    cut: list[Commit] = []
    for commit in commits:
        if commit.files not in by_files:
            if budget_s is not None and clock() - started >= budget_s:
                cut.append(commit)
                continue
            by_files[commit.files] = select(list(commit.files))
        reach[commit.sha] = by_files[commit.files]
    for attribution in by_module.values():
        for commit in commits:
            if attribution.module in commit.files:
                why = "edits the failing test file"
            elif commit.sha not in reach:
                continue
            elif (selected := reach[commit.sha]) is None:
                why = "global: the selector cannot narrow this commit, so it reaches every test"
            elif attribution.module in selected:
                why = "the selector maps this commit's files to the failing module"
            else:
                continue
            attribution.candidates.append(Candidate(commit.sha, commit.pr, commit.subject, why))
    return list(by_module.values()), cut


def candidate_shas(attributions: list[Attribution]) -> list[tuple[str, int | None]]:
    """Every candidate once: direct ones (file or selector) before global ones, newest first within each."""
    direct = [(c.sha, c.pr) for a in attributions for c in a.candidates if not c.why.startswith("global")]
    wide = [(c.sha, c.pr) for a in attributions for c in a.candidates if c.why.startswith("global")]
    return list(dict.fromkeys(direct + wide))


def annotation_line(
    chosen_range: dict, attributions: list[Attribution], failing: list[str], unattributed: int = 0
) -> str:
    """R4: the one annotation the fleet-af checkpoint owner parses (`candidates=` and `ids=` are comma separated);
    R6: `unattributed=<k>` follows when the budget cut k commits out of the candidate search."""
    rendered = ",".join(f"{sha}#{pr}" if pr else sha for sha, pr in candidate_shas(attributions)) or "none"
    shown = failing[:ANNOTATION_IDS]
    data = (
        f"range={chosen_range['base']}..{chosen_range['head']} candidates={rendered} failing={len(failing)} "
        f"ids={','.join(shown) or 'none'}"
    )
    if len(failing) > len(shown):
        data += f" ids_omitted={len(failing) - len(shown)}"
    if unattributed:
        data += f" unattributed={unattributed}"
    return f"::error title={ANNOTATION_TITLE}::{_command_data(data)}"


def _command_data(value: str) -> str:
    """Workflow-command data escaping, per the GitHub Actions toolkit (a node id may carry `%`)."""
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def cut_line(cut: list[Commit], attributed: int, budget_s: float) -> str:
    """R6: the warning naming what the attribution budget cut, so a partial candidate list never reads as whole."""
    shas = ",".join(c.sha for c in cut)
    data = (
        f"attribution cut after {attributed} of {attributed + len(cut)} commit(s) (budget {budget_s:g}s); "
        f"unattributed={shas}"
    )
    return f"::warning title={CUT_TITLE}::{data}"


def inconclusive_line(chosen_range: dict, reason: str, completed: int) -> str:
    """R5: the warning the fleet-af checkpoint owner reads as inconclusive (`result=` and `reason=` first)."""
    data = f"result=inconclusive reason={reason} range={chosen_range['base']}..{chosen_range['head']} completed={completed}"
    return f"::warning title={INCONCLUSIVE_TITLE}::{data}"


def summary_markdown(
    record: dict,
    selection: dict | None,
    outcome: str,
    failing: list[str],
    attributions: list[Attribution],
    cut: list[Commit] | None = None,
    budget_s: float = ATTRIBUTION_BUDGET_S,
) -> str:
    return summary_head(record, selection, outcome) + summary_failure(outcome, failing, attributions, cut, budget_s)


def summary_head(record: dict, selection: dict | None, outcome: str) -> str:
    """R6: the range, the selection and the commits, written before any attribution starts."""
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
    return "\n".join(lines) + "\n"


def summary_failure(
    outcome: str,
    failing: list[str],
    attributions: list[Attribution],
    cut: list[Commit] | None = None,
    budget_s: float = ATTRIBUTION_BUDGET_S,
) -> str:
    """The failing ids and the candidates; R6: then what the attribution budget cut, by sha."""
    if not outcome.startswith("FAIL"):
        return ""
    lines = ["", f"Failing tests ({len(failing)}):", ""] + [f"- `{t}`" for t in failing]
    lines += ["", "Culprit candidates (commits in the range the selector maps to each failing module):", ""]
    for a in attributions:
        lines.append(f"- `{a.module}`:")
        shown = a.candidates[:SUMMARY_CANDIDATES]
        lines += [
            f"  - `{c.sha[:10]}` {('#' + str(c.pr) + ' ') if c.pr else ''}{c.subject} ({c.why})" for c in shown
        ] or ["  - none in this range (an older merge, a flake, or a host problem)"]
        if len(a.candidates) > len(shown):
            lines.append(f"  - and {len(a.candidates) - len(shown)} more in culprits.json")
    if cut:
        lines += [
            "",
            f"Cut: the report's {budget_s:g}s attribution budget ran out, so these {len(cut)} older commit(s) were "
            "not mapped by the selector (only an edit of a failing test file names them above):",
            "",
        ] + [f"- `{c.sha[:10]}` {('#' + str(c.pr) + ' ') if c.pr else ''}{c.subject}" for c in cut]
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
    merge maps to the tests that read that workflow instead of to every test; `ci/*` stays global.
    R6: the import graph is loaded once, on the first call, and shared by every commit (run 42188076608 loaded it
    27 times, two thirds of the 5 minutes). A graph the selector cannot read (Graph.load raises FullSuite, e.g. a
    conftest computing `pytest_plugins`) makes every commit global, as a per-call load did, and is read once."""
    policy = Policy(ci_config_by_name=True)
    graphs: list[Graph | None] = []

    def select(paths: list[str]) -> set[str] | None:
        if not paths:
            return set()
        try:
            if not graphs:
                graphs.append(None)  # stays None when the load below raises: unreadable, never retried
                graphs[0] = Graph.load(REPO)
            if graphs[0] is None:
                return None
            return set(select_tests(paths, ignores, REPO, policy, graphs[0]).modules)
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


def _log(line: str) -> None:
    """R6: a progress line in the step log, flushed at once so a killed step still shows how far it got."""
    print(f"[main-hourly] {line}", flush=True)


def _summarize(text: str) -> None:
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(text)
    else:
        print(text, flush=True)


def _report(arguments: argparse.Namespace) -> int:
    record = json.loads(Path(arguments.range).read_text(encoding="utf-8"))
    selection_path = Path(arguments.selection) if arguments.selection else None
    selection = (
        json.loads(selection_path.read_text(encoding="utf-8")) if selection_path and selection_path.is_file() else None
    )
    hourly_record = record["hourly"]
    _log(f"range {(record['base'] or '-')}..{record['head']}: {len(hourly_record['commits'])} commit(s)")
    if selection is not None:
        _log(f"selection {len(selection.get('selected_modules') or [])} module(s), mode {selection.get('mode')}")
    rc = arguments.pytest_rc
    failing: list[str] = []
    attributions: list[Attribution] = []
    cut: list[Commit] = []
    junit = Path(arguments.junit) if arguments.junit else None
    cases = recorded_cases(junit)
    inconclusive = False
    resolved: list[str] = []
    if hourly_record["empty"]:
        outcome = "EMPTY (nothing merged since the last tested tip)"
    elif rc == 0 and not arguments.wall_cap:
        outcome = "PASS"
    else:
        failing = failing_ids_of(cases)
        resolved = [t for t in failing if not t.startswith(UNRESOLVED)]
        if arguments.wall_cap and not failing:
            inconclusive = True
            outcome = (
                f"INCONCLUSIVE, result=inconclusive reason=wall-cap (pytest rc={rc}: the wall-time cap stopped the "
                f"run before the selection finished, and its {len(cases)} completed test(s) have no failure)"
            )
        elif not resolved:
            said = (
                f"its {len(failing)} failing test(s) map to no file in this checkout"
                if failing
                else "the JUnit report names no failing test"
            )
            outcome = f"FAIL, failing tests UNKNOWN (pytest rc={rc}, {said})"
        else:
            outcome = f"FAIL ({len(failing)} failing test id(s))"
    _summarize(summary_head(record, selection, outcome))
    if resolved:
        commits = [
            Commit(c["sha"], c["committed_epoch"], c["subject"], c["pr"], tuple(c["files"]))
            for c in hourly_record["commits"]
        ]
        _log(f"{len(failing)} failing id(s); attributing {len(commits)} commit(s), budget {arguments.budget_s:g}s")
        attributions, cut = attribute_within(resolved, commits, lane_selector(arguments.ignore), arguments.budget_s)
    _summarize(summary_failure(outcome, failing, attributions, cut, arguments.budget_s))
    Path(arguments.out).write_text(
        json.dumps(
            {
                "range": {"base": record["base"], "head": record["head"], "source": hourly_record["source"]},
                "outcome": outcome,
                "result": "inconclusive" if inconclusive else None,
                "reason": "wall-cap" if inconclusive else None,
                "pytest_rc": rc,
                "failing": failing,
                "attributions": [asdict(a) for a in attributions],
                "candidates": [{"sha": sha, "pr": pr} for sha, pr in candidate_shas(attributions)],
                "unattributed": [c.sha for c in cut],
            },
            indent=1,
        )
        + "\n",
        encoding="utf-8",
    )
    if cut:
        print(cut_line(cut, len(hourly_record["commits"]) - len(cut), arguments.budget_s))
    if inconclusive:
        print(inconclusive_line(record, "wall-cap", len(cases)))
        return 0
    if hourly_record["empty"]:
        return rc
    if arguments.wall_cap:  # a failing id in the partial report: red with culprits, whatever the timeout's rc
        print(annotation_line(record, attributions, failing, len(cut)))
        return 1
    if rc != 0:
        print(annotation_line(record, attributions, failing, len(cut)))
    return rc


@dataclass(frozen=True)
class Recorded:
    """One named JUnit testcase: what node_id needs, and whether it carries a <failure> or <error>."""

    classname: str
    name: str
    file: str
    failed: bool


def recorded_cases(junit: Path | None) -> list[Recorded]:
    """R5: the named test cases the (possibly partial) JUnit report records. SIGINT leaves the interrupted test
    as a nameless `<testcase/>`, which records nothing; an absent or truncated report records nothing either.
    R6: stream-parsed, each testcase cleared once read, so a report of thousands of cases stays small."""
    if not junit or not junit.is_file():
        return []
    cases: list[Recorded] = []
    try:
        # Same trust as ci_junit_failures.failing_ids: this job's own pytest output, no external entities.
        # nosemgrep: python.lang.security.use-defused-xml-parse.use-defused-xml-parse
        for _, element in ElementTree.iterparse(junit, events=("end",)):
            if element.tag != "testcase":
                continue
            if element.get("name"):
                failed = element.find("failure") is not None or element.find("error") is not None
                cases.append(
                    Recorded(element.get("classname", ""), element.get("name", ""), element.get("file", ""), failed)
                )
            element.clear()
    except ElementTree.ParseError:  # pytest stopped mid-write: no test in it is recorded, said by completed=0
        return []
    return cases


def failing_ids_of(cases: list[Recorded]) -> list[str]:
    """R5: every recorded failure or error (pytest-timeout ones included) as unique node ids in report order; an
    unmappable one keeps ci_junit_failures' `UNRESOLVED ` prefix."""
    ids = [node_id(case.classname, case.name, REPO, case.file) for case in cases if case.failed]
    return list(dict.fromkeys(ids))


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
    report.add_argument(
        "--budget-s",
        type=float,
        default=ATTRIBUTION_BUDGET_S,
        help="R6: seconds of per-commit attribution before the rest of the range is named as cut",
    )
    arguments = parser.parse_args(argv)
    return _range(arguments) if arguments.command == "range" else _report(arguments)


if __name__ == "__main__":
    sys.exit(main())
