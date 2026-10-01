"""Which tests a pull request HEAD runs in the five pytest fast lane shards (DEVOPS-20).

the maintainer, Thu 1 Oct 2026, answered in chat: "Go now, skip the trial". A PR head runs only the
tests its change affects; the Trunk merge queue draft, every main push, every
`workflow_dispatch` and every `ci:trunk-repair` PR keep running the FULL suite, so every
merge is still fully tested on the exact tree that lands. Decision record:
docs/decisions/ADR-NEW-pr-heads-run-affected-tests.md.

Mode, decided once per shard from the same inputs, so every shard agrees:

    full      the job runs today's full sharded lane, unchanged.
    affected  this shard runs the selected test modules assigned to it.
    empty     the selection, or this shard's slice of it, is explicitly empty: the shard
              reports success without starting pytest, and says why, loudly.

Selection happens ONLY when all of these hold, and the reason for the first one that does
not is logged and recorded:
  1. repo variable `CI_PR_TEST_SELECTION` equals `affected` (the kill switch; anything else,
     unset included, is the full suite);
  2. the event is `pull_request`;
  3. the head ref does not start with `trunk-merge/` (a Trunk queue draft);
  4. the PR does not carry the `ci:trunk-repair` label.

A PR head then falls back to FULL, never to an empty run, when a changed path is global
(`GLOBAL_PATTERNS`), when the import graph cannot resolve a changed Python path (the
UNKNOWN of `scripts.affected_tests`), when changed code is imported by a top-level
conftest, or when the selection cannot be computed for any reason at all. Every fallback
names its cause in the log, the job summary and the record.

What is selected, and why each rule exists: scripts/ci_test_selection_rules.py.

Shards: the selected modules are spread over the shards by recorded duration
(`.test_durations`), greedy least-loaded first, so no shard does all the work and every
shard computes the same assignment.

Usage (the shard step in .github/workflows/ci.yml):
    python -m scripts.ci_test_selection select --shard 1 --shards 5 --out-dir DIR \\
        --ignore=tests/analysis ...
    python -m scripts.ci_test_selection junit-all-skipped REPORT.xml --modules DIR/shard-modules.txt

`select` prints exactly one line on stdout, the mode, and writes DIR/selection.json (the
record the escape measurement reads, scripts/ci_selection_escapes.py) and, in affected
mode, DIR/shard-modules.txt. Everything for humans goes to stderr and the job summary.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import traceback
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree

from scripts.affected_tests import REPO
from scripts.ci_test_selection_rules import DURATIONS_NAME, FullSuite, Selection, select_tests

SWITCH_VARIABLE = "CI_PR_TEST_SELECTION"
SWITCH_ON = "affected"
QUEUE_DRAFT_PREFIX = "trunk-merge/"
REPAIR_LABEL = "ci:trunk-repair"
SCHEMA = 1
RECORD_NAME = "selection.json"
SHARD_MODULES_NAME = "shard-modules.txt"


# ---------------------------------------------------------------------------
# event classification and the kill switch
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    name: str
    head_ref: str
    head_sha: str
    labels: tuple[str, ...]
    pr_number: int | None

    @classmethod
    def from_payload(cls, name: str, payload: dict) -> Event:
        pull_request = payload.get("pull_request") or {}
        head = pull_request.get("head") or {}
        return cls(
            name=name,
            head_ref=str(head.get("ref") or ""),
            head_sha=str(head.get("sha") or ""),
            labels=tuple(str(label["name"]) for label in pull_request.get("labels") or ()),
            pr_number=pull_request.get("number"),
        )


def full_suite_reason(switch: str | None, event: Event) -> str | None:
    """Why this run takes the full suite, or None when it is a PR head with selection on."""
    if event.name != "pull_request":
        return f"event `{event.name}` is not a pull request head; only PR heads select"
    if event.head_ref.startswith(QUEUE_DRAFT_PREFIX):
        return f"head `{event.head_ref}` is a Trunk merge-queue draft; the queue runs everything"
    if REPAIR_LABEL in event.labels:
        return f"the PR carries `{REPAIR_LABEL}`; a trunk repair runs everything"
    if switch != SWITCH_ON:
        return (
            f"repo variable {SWITCH_VARIABLE}={switch!r}; selection runs only when it "
            f"equals {SWITCH_ON!r}"
        )
    return None


# ---------------------------------------------------------------------------
# shard assignment
# ---------------------------------------------------------------------------


def module_durations(ledger: dict[str, float]) -> dict[str, float]:
    """Recorded seconds per test module, summed over its node ids."""
    totals: dict[str, float] = {}
    for node, seconds in ledger.items():
        module = node.split("::", 1)[0]
        totals[module] = totals.get(module, 0.0) + float(seconds)
    return totals


def assign_shards(modules: list[str], durations: dict[str, float], shards: int) -> list[list[str]]:
    """Greedy least-loaded assignment, heaviest module first; ties go to the lower shard.

    A module the ledger has never timed (a new test file) weighs the median recorded module,
    so it is neither free nor dominant.
    """
    known = [durations[m] for m in modules if m in durations]
    default = statistics.median(known) if known else 1.0
    weights = {m: durations.get(m, default) for m in modules}
    loads = [0.0] * shards
    assignment: list[list[str]] = [[] for _ in range(shards)]
    for module in sorted(modules, key=lambda m: (-weights[m], m)):
        target = min(range(shards), key=lambda i: (loads[i], i))
        assignment[target].append(module)
        loads[target] += weights[module]
    return [sorted(group) for group in assignment]


# ---------------------------------------------------------------------------
# the PR diff
# ---------------------------------------------------------------------------


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()


def pr_changed_paths(root: Path, head_sha: str) -> list[str]:
    """Paths the PR changes against its merge base, read from the PR merge commit checkout.

    `actions/checkout` on a pull_request checks out the test merge commit, whose second
    parent is the PR head. That is verified, not assumed: a checkout of anything else
    cannot say what the PR changed. `--no-renames` lists a rename's old path too.
    """
    second_parent = _git(root, "rev-parse", "HEAD^2")
    if second_parent != head_sha:
        raise FullSuite(
            f"the checkout's HEAD^2 is {second_parent}, not the PR head {head_sha}, so the "
            "PR's own change set cannot be read from it"
        )
    merge_base = _git(root, "merge-base", "HEAD^1", "HEAD^2")
    diff = _git(root, "diff", "--name-only", "--no-renames", merge_base, "HEAD^2")
    return [line for line in diff.splitlines() if line]


# ---------------------------------------------------------------------------
# record, log and summary
# ---------------------------------------------------------------------------


@dataclass
class Record:
    schema: int
    mode: str
    reason: str
    switch: str | None
    event: str
    head_ref: str
    head_sha: str
    pr_number: int | None
    changed_paths: list[str]
    selected_modules: list[str]
    shards: dict[str, list[str]]
    reasons: dict[str, list[str]]
    generated_at: str


def decide(switch: str | None, event: Event, root: Path, ignores: list[str], shards: int) -> Record:
    """The whole decision for this run. Never raises: any failure is a FULL record."""
    changed: list[str] = []

    def record(
        mode: str,
        reason: str,
        selection: Selection | None = None,
        assignment: list[list[str]] | None = None,
    ) -> Record:
        return Record(
            schema=SCHEMA,
            mode=mode,
            reason=reason,
            switch=switch,
            event=event.name,
            head_ref=event.head_ref,
            head_sha=event.head_sha,
            pr_number=event.pr_number,
            changed_paths=changed,
            selected_modules=selection.modules if selection else [],
            shards={str(i + 1): group for i, group in enumerate(assignment or [])},
            reasons=selection.reasons if selection else {},
            generated_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

    reason = full_suite_reason(switch, event)
    if reason:
        return record("full", reason)
    try:
        changed = pr_changed_paths(root, event.head_sha)
        selection = select_tests(changed, ignores, root)
        ledger = json.loads((root / DURATIONS_NAME).read_text(encoding="utf-8"))
        assignment = assign_shards(selection.modules, module_durations(ledger), shards)
    except FullSuite as fallback:
        return record("full", f"fell back to the full suite: {fallback}")
    except Exception as error:  # reported loudly below, and the fallback runs MORE tests
        traceback.print_exc(file=sys.stderr)
        return record(
            "full", f"fell back to the full suite: selection could not be computed: {error!r}"
        )
    if not selection.modules:
        return record(
            "affected", "no test module is affected by this change", selection, assignment
        )
    return record(
        "affected", f"{len(selection.modules)} affected test module(s)", selection, assignment
    )


def shard_mode(record: Record, shard: int) -> str:
    if record.mode == "full":
        return "full"
    return "affected" if record.shards.get(str(shard)) else "empty"


def report_lines(record: Record, shard: int, shards: int) -> list[str]:
    mode = shard_mode(record, shard)
    lines = [f"PR TEST SELECTION: shard {shard} of {shards} runs mode={mode.upper()}"]
    lines.append(f"  reason: {record.reason}")
    lines.append(
        f"  {SWITCH_VARIABLE}={record.switch!r}, event={record.event}, head={record.head_ref}"
    )
    if record.mode == "affected":
        mine = record.shards.get(str(shard), [])
        lines.append(
            f"  selected {len(record.selected_modules)} module(s) for {len(record.changed_paths)} "
            f"changed path(s); this shard runs {len(mine)}"
        )
        lines += [f"    {module}" for module in mine]
    if mode == "empty":
        lines.append(
            "  ZERO affected tests for this shard: it reports success without running pytest. "
            "The Trunk merge queue runs the FULL suite on the exact tree that lands."
        )
    return lines


def summary_markdown(record: Record, shard: int, shards: int) -> str:
    mode = shard_mode(record, shard)
    rows = [
        f"### pytest fast lane (shard {shard} of {shards}): selection mode **{mode.upper()}**",
        "",
        "| field | value |",
        "|:--|:--|",
        f"| reason | {record.reason} |",
        f"| `{SWITCH_VARIABLE}` | `{record.switch}` |",
        f"| event / head | `{record.event}` / `{record.head_ref}` |",
        f"| changed paths | {len(record.changed_paths)} |",
        f"| selected modules (all shards) | {len(record.selected_modules)} |",
        f"| this shard's modules | {len(record.shards.get(str(shard), []))} |",
        "",
    ]
    if mode == "empty":
        rows.append(
            "Zero affected tests for this shard, so pytest did not start. The merge queue "
            "still runs the full suite before anything lands."
        )
    elif mode == "affected":
        rows.append("<details><summary>this shard's modules</summary>\n")
        rows += [f"- `{m}`" for m in record.shards[str(shard)]]
        rows.append("\n</details>")
    return "\n".join(rows) + "\n"


# ---------------------------------------------------------------------------
# JUnit evidence for exit 5
# ---------------------------------------------------------------------------


def _dotted(module_path: str) -> str:
    return module_path.removesuffix(".py").replace("/", ".")


def junit_all_skipped(report: Path, modules: list[str]) -> bool:
    """True only when EVERY selected module skipped itself at module level, and nothing else ran.

    pytest exits 5 when it collects no test items. A module that skips itself at module level
    (`pytest.skip(..., allow_module_level=True)`) collects none and leaves one testcase named
    by its dotted path with a <skipped> child; a module with no tests leaves nothing at all.
    So exit 5 is excused only when each selected module has that skipped testcase and the
    report holds no other outcome, which is what the full lane reports green for them.
    """
    if not modules:
        return False
    cases = ElementTree.parse(report).getroot().findall(".//testcase")
    if not cases or not all(case.find("skipped") is not None for case in cases):
        return False
    skipped_modules = {case.get("name", "") for case in cases if not case.get("classname")}
    return all(_dotted(module) in skipped_modules for module in modules)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _append(path_env: str, text: str) -> None:
    target = os.environ.get(path_env)
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(text)


def _select(arguments: argparse.Namespace) -> int:
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    payload = json.loads(Path(event_path).read_text(encoding="utf-8")) if event_path else {}
    event = Event.from_payload(event_name, payload)
    switch = os.environ.get(SWITCH_VARIABLE)
    record = decide(switch, event, REPO, arguments.ignore, arguments.shards)

    out_dir = Path(arguments.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / RECORD_NAME).write_text(
        json.dumps(asdict(record), indent=2) + "\n", encoding="utf-8"
    )
    mode = shard_mode(record, arguments.shard)
    if mode == "affected":
        (out_dir / SHARD_MODULES_NAME).write_text(
            "\n".join(record.shards[str(arguments.shard)]) + "\n", encoding="utf-8"
        )
    for line in report_lines(record, arguments.shard, arguments.shards):
        print(line, file=sys.stderr)
    _append("GITHUB_STEP_SUMMARY", summary_markdown(record, arguments.shard, arguments.shards))
    print(mode)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    select = commands.add_parser("select", help="decide this shard's mode and modules")
    select.add_argument("--shard", type=int, required=True)
    select.add_argument("--shards", type=int, required=True)
    select.add_argument("--out-dir", required=True)
    select.add_argument("--ignore", action="append", default=[], help="a lane --ignore path")
    junit = commands.add_parser(
        "junit-all-skipped", help="exit 0 iff every selected module skipped at module level"
    )
    junit.add_argument("report")
    junit.add_argument("--modules", required=True, help="the shard's shard-modules.txt")
    arguments = parser.parse_args(argv)
    if arguments.command == "select":
        if not 1 <= arguments.shard <= arguments.shards:
            parser.error(f"--shard {arguments.shard} is outside 1..{arguments.shards}")
        return _select(arguments)
    if arguments.command == "junit-all-skipped":
        modules = Path(arguments.modules).read_text(encoding="utf-8").split()
        return 0 if junit_all_skipped(Path(arguments.report), modules) else 1
    parser.error(f"unknown command {arguments.command!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
