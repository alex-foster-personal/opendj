"""Ratchet scorecard: is each CI gate earning the delay it costs?

the maintainer, Thu 15 Oct 2026: check every day whether each quality ratchet is helping or
hindering, how much time it ate and what it bought, and whether it could become a
warning or an after-merge fix instead of a red CI fail. This REPORTS and never gates.
Decision record: (private path).

What it measures, from GitHub Actions only (no repository code is run):

    gate key      one thing that can turn a PR red. A quality metric
                  (`quality:mypy.errors_tests`, from the `[quality] REGRESSION` lines), a
                  failing pytest MODULE (`pytest:tests/x/test_y.py`), or a non-pytest job
                  and its failed step (`frontend unit + check + build: Bundle budgets ...`).
    class         `ratchet` (a rule about the code: quality metrics, budgets, contract
                  drift, guard tests that read the tree) or `behavior` (a test of what the
                  product does). `RATCHET_TESTS` is the list; a module not on it is behavior.
    sole red      a PR head whose ONLY named failures are ratchets. That PR was held back
                  by rules alone, so a warn tier would have let it into the queue.
    held hours    for a sole-red head, the time until the branch's next CI verdict, capped
                  at `HELD_CAP_H`, split evenly across the ratchets that were red together.
    fixes         for each red key, the next head on the same branch where it is no longer
                  red, and the files that branch's OWN commits changed in between (merge
                  commits from main are skipped). `code` means product code changed,
                  `paperwork` means only allowances, generated files and docs changed,
                  `tests` means only tests changed.

Verdict per gate (`verdict`), all stated with their counts so a reader can disagree:

    QUIET          red on fewer than `MIN_BRANCHES` branches: no evidence either way, keep.
    KEEP           a behavior gate, or a ratchet whose known fixes were mostly code.
    AFTER-MERGE    a ratchet whose known fixes were mostly paperwork: a regeneration or an
                   allowance bump that a bot on main can do, so the PR should not wait.
    WARN           a ratchet that held PRs on its own (sole red), with at least `MIN_FIXES`
                   readable fixes, not mostly code: demote to a non-blocking warning plus
                   a debt entry. Fewer readable fixes than that is KEEP (fail closed).

"Caught a defect" is INFERRED from what the fix changed, never measured: a code change
after a red is consistent with a real catch and also with a refactor done to satisfy a
count. The scorecard says so in its output.

Exit codes: 0 measured; 3 UNKNOWN (no completed run in the window, or every log of a
failed gating job was unreadable). A gate is never scored from an unreadable log.

Usage:
    python -m scripts.ratchet_scorecard --days 7 --json out.json --markdown out.md
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from scripts.ci_failure_ids import failed_identities, failure_beyond_tests
from scripts.quality_gate import HARD_ZERO

REPO = "private_owner/music-dj-tools"
TRUNK_BOT = "trunk-io[bot]"
WORKFLOWS = ("ci.yml", "e2e.yml")
# Jobs that only aggregate, record or feed other jobs. Their failure is never a gate of its
# own: `ci gate` and `e2e verdict` restate the jobs they need, CI Insights and the Trunk
# uploads are telemetry, and the quarantine list fails closed into the shards it feeds.
NON_GATE_JOBS = frozenset(
    {
        "ci gate",
        "e2e verdict",
        "CI Insights",
        "Trunk e2e root report",
        "Trunk quarantined tests",
        # continue-on-error on pull_request: an early signal, never a verdict.
        "pytest fast tier",
    }
)
PYTEST_JOBS = frozenset({"pytest fast lane"})
QUALITY_JOB = "quality ratchet"
# The jobs that restate a workflow's verdict. One failing while no gating job did is a
# verdict nothing measured (its own checkout, its evaluator), never a green head.
AGGREGATE_JOBS = frozenset({"ci gate", "e2e verdict"})
# The step whose run is what a log-read job's keys measure (ci.yml step names, by prefix).
# The job whose docs-only decision is the one legitimate reason `quality` skips on main.
MEASURING_STEPS = {"pytest fast lane": "Run fast pytest lane shard", QUALITY_JOB: "Quality ratchet"}
# Steps that are ratchets even though they live outside the quality job.
RATCHET_STEPS = re.compile(r"Bundle budgets|Contract drift|reqs\.json|reqs-check")
# Pytest modules that are rules about the tree rather than tests of the product. Prefix
# match on the module path. Additions are cheap; a module left off reads as behavior,
# which is the conservative direction (behavior gates always KEEP).
RATCHET_TESTS = (
    "tests/quality/",
    "(private path)",
    "(private path)",
    "tests/scripts/test_oss_tip_audit",
    "tests/scripts/test_shell_construct_lint.py",
    "(private path)",
    "(private path)",
    "(private path)",
    "tests/test_claude_md",
    "tests/test_adr",
    "(private path)",
    "tests/docs/",
)
# Files whose change is bookkeeping, not product: allowances, generated artifacts, docs.
PAPERWORK = re.compile(
    r"^(ops/quality/baseline\.json|reqs\.json|apps/webui/openapi\.json"
    r"|apps/webui/frontend/src/lib/api-types\.ts|\.planning/|docs/|\.test_durations"
    r"|apps/webui/frontend/scripts/bundle-budget\.mjs|\.importlinter|scripts/oss_tip_rules\.py"
    r"|\.agents/|.*\.md$)"
)
TEST_PATH = re.compile(r"(^tests/|/tests/|\.test\.|\.spec\.)")
HELD_CAP_H = 24.0
SLICE_H = 6
MIN_BRANCHES = 3
MIN_FIXES = 3
# GitHub serves at most 3000 files of one commit; beyond that the list is truncated.
MAX_FILE_PAGES = 30
# GitHub's compare endpoint lists at most this many files and does not page them.
MAX_COMPARE_FILES = 300
DEFAULT_DAYS = 1.0
# How far before the window red heads are read, so a fix landing after `since` is scored.
# A red head belongs to the one window holding its full verdict (both workflows finished).
# The daily run scores the window that ended LOOKAHEAD ago, reading runs up to now, so a fix
# up to a day after the red is still its fix; LOOKBEHIND reaches runs created before the
# window that finish inside it.
LOOKAHEAD = dt.timedelta(days=1)
# GitHub cancels a self-hosted job queued for 24 hours, and a job only queues once the jobs
# it needs have finished, so a run can finish (24 h + timeout) per job on its longest `needs`
# chain after it was created: 97.4 h for ci.yml and 97.6 h for e2e.yml on Fri 16 Oct 2026,
# up from 96.75 h and 73.3 h a day earlier, so this keeps a margin for chains to grow. A test
# recomputes the bound from the workflow files and fails when this falls short.
# A re-run keeps its run's created_at, so re-running a run created more than LOOKBEHIND
# before the window is not read at all: that sha is absent from the card, not incomplete.
# Accepted (see the ADR): GitHub filters runs by creation only, so catching it means listing
# all history every day, for a manual re-run of a PR head at least five days old.
LOOKBEHIND = dt.timedelta(hours=120)
CODE_SHARE_KEEP = 0.5
PAPERWORK_SHARE_AFTER_MERGE = 0.6

Api = Callable[[str], str]


# ----- pure core -----------------------------------------------------------------------


def quality_keys(lines: Iterable[str]) -> list[str]:
    """`quality:<metric>` for every REGRESSION line in a quality job log, and
    `quality-unknown:<metric>` for every UNKNOWN one: a measurement that could not be
    taken says nothing about the rule, so it is never scored as the ratchet."""
    keys: set[str] = set()
    for line in lines:
        match = re.search(r"\[quality\] (REGRESSION|UNKNOWN):?\s+([A-Za-z][A-Za-z0-9_.\-]*)", line)
        if match:
            prefix = "quality:" if match.group(1) == "REGRESSION" else "quality-unknown:"
            keys.add(prefix + match.group(2).rstrip(":"))
    return sorted(keys)


def pytest_keys(identities: Iterable[str]) -> list[str]:
    """`pytest:<module>` for every failing pytest identity; node ids collapse to modules."""
    keys = set()
    for identity in identities:
        match = re.match(r"(?:FAILED|ERROR) (tests/\S+?\.py)", identity)
        if match:
            keys.add("pytest:" + match.group(1))
    return sorted(keys)


SELECTION_LINE = re.compile(r"PR TEST SELECTION: shard \d+ of \d+ runs mode=(FULL|AFFECTED|EMPTY)")
SELECTED_MODULE = re.compile(r"^(?:\S+Z )?    (tests/\S+)\s*$")


def shard_selection(log: str | None) -> frozenset[str] | None:
    """The modules one pytest shard ran, from scripts/ci_test_selection.py's report: None
    for its full slice, the listed modules for AFFECTED, none for EMPTY. A log that cannot
    be read or carries no report proves nothing ran."""
    match = SELECTION_LINE.search(log or "")
    if not match:
        return frozenset()
    if match.group(1) == "FULL":
        return None
    lines = (log or "")[match.end() :].splitlines()
    return frozenset(m.group(1) for line in lines if (m := SELECTED_MODULE.match(line)))


def selection_unread(log: str | None) -> bool:
    """True when a shard's log carries no selection report: unread, cut short, or a report
    format this parser no longer knows. Only an explicit mode=EMPTY says the shard ran
    nothing; a missing report says nothing about what ran."""
    return not SELECTION_LINE.search(log or "")


def unread_selections(
    jobs: Sequence[Mapping], selection: Mapping[int, str | None], logs: Mapping[int, str | None]
) -> int:
    """How many of a run's consulted shards carry no selection report: a passing shard read
    for its selection, or a failed shard whose readable failure log (in `logs`) printed none.
    A failed shard's empty log is already an unread log and is not counted twice."""
    return sum(
        (j["id"] in selection and selection_unread(selection[j["id"]]))
        or (
            j["id"] not in selection
            and j["name"].split(" (")[0] in PYTEST_JOBS
            and bool(logs.get(j["id"]))
            and selection_unread(logs.get(j["id"]))
        )
        for j in jobs
    )


def run_selection(jobs: Sequence[Mapping], logs: Mapping[int, str | None]) -> frozenset[str] | None:
    """The pytest modules a run executed across its shards; None only when every shard
    that ran reported its full slice (pytest-split spreads a module to one shard)."""
    shards = [j for j in jobs if j["name"].split(" (")[0] in PYTEST_JOBS and j["conclusion"] != "skipped"]
    picked = [shard_selection(logs.get(j["id"])) for j in shards]
    if shards and all(p is None for p in picked):
        return None
    return frozenset().union(*(p for p in picked if p is not None))


def job_keys(name: str, failed_steps: Sequence[str], log: str | None) -> list[str]:
    """Every gate key one failed job names. `log` is None for a job read by steps alone."""
    base = name.split(" (", maxsplit=1)[0]
    if base == QUALITY_JOB and log is not None:
        # No REGRESSION or UNKNOWN line: the job failed before measuring (setup, checkout,
        # a timeout), which says nothing about any rule.
        return quality_keys(log.splitlines()) or [f"quality-unknown:{' | '.join(failed_steps) or '?'}"]
    if base in PYTEST_JOBS and log is not None:
        keys = pytest_keys(failed_identities(log)) or ["pytest:<no identity>"]
        # A shard that named failures and was then killed (timeout 124, OOM 137, a cancel)
        # never ran what came after them: its named tests are a floor, not its verdict.
        return [*keys, "pytest:<beyond tests>"] if failure_beyond_tests(log) else keys
    # One key per failed step: a ratchet step failing beside a product step must not hide it.
    return [f"{base}: {step}" for step in failed_steps] or [f"{base}: <no failed step>"]


def judge_jobs(jobs: Sequence[Mapping], logs: Mapping[int, str | None]) -> tuple[frozenset[str], bool, int]:
    """One run's verdict from its jobs: (red gate keys, measured, unread logs). A gating
    job that ended without a verdict (timed_out, cancelled, startup_failure), an unread
    log, or a quality measurement that could not be taken leaves the run unmeasured, so it
    can neither be sole red nor clear an earlier red."""
    red: set[str] = set()
    measured = True
    missing = 0
    aggregate_failed = False
    for job in jobs:
        base = job["name"].split(" (")[0]
        aggregate_failed |= base in AGGREGATE_JOBS and job["conclusion"] == "failure"
        if job["conclusion"] in ("success", "skipped", "neutral") or base in NON_GATE_JOBS:
            continue
        if job["conclusion"] != "failure":
            measured = False
            continue
        failed_steps = [s["name"] for s in job.get("steps", []) if s["conclusion"] == "failure"]
        needs_log = base in PYTEST_JOBS or base == QUALITY_JOB
        log = logs.get(job["id"]) if needs_log else None
        if needs_log and log is None:
            missing += 1
            measured = False
            continue
        red.update(job_keys(job["name"], failed_steps, log))
    if any(gate_class(k) == "unmeasured" for k in red) or (aggregate_failed and not red and measured):
        measured = False
    return frozenset(red), measured, missing


def skipped_jobs(jobs: Sequence[Mapping]) -> frozenset[str]:
    """What a run did not get to judge: the base names of the gating jobs it skipped, and
    every step a gating job skipped, as its `job: step` key (after a failure GitHub runs later
    steps only on `success()`; a step's own `if:` skips it too). A pytest or quality job's
    keys come from its log, so it counts as skipped whole only when its measuring step did
    not run; an upload or ledger download it skipped judges nothing."""
    out: set[str] = set()
    for j in jobs:
        base = j["name"].split(" (")[0]
        if base in NON_GATE_JOBS:
            continue
        if j["conclusion"] == "skipped":
            out.add(base)
            continue
        steps = j.get("steps", [])
        if base in MEASURING_STEPS:
            measuring = [st for st in steps if st["name"].startswith(MEASURING_STEPS[base])]
            if not measuring or any(st["conclusion"] in ("skipped", "cancelled") for st in measuring):
                out.add(base)
        out.update(f"{base}: {st['name']}" for st in steps if st["conclusion"] == "skipped")
    return frozenset(out)


def origin_job(key: str) -> str:
    """The job whose verdict a gate key comes from: a later run clears the key only if it ran that job."""
    if key.startswith(("quality:", "quality-unknown:")):
        return QUALITY_JOB
    if key.startswith("pytest:"):
        return next(iter(PYTEST_JOBS))
    return key.split(": ", 1)[0]


def run_gave_verdict(conclusion: str, jobs: Sequence[Mapping]) -> bool:
    """False for a run that ended in startup_failure, stale or action_required, or failed
    with no jobs to read: it gave no verdict, and reading it as green would invent one."""
    if conclusion in ("success", "skipped"):
        return True
    return conclusion == "failure" and bool(jobs)


def gate_class(key: str) -> str:
    # A pytest log naming no failed test is a shard that never finished (killed by its wall
    # timeout, a crashed worker): its absent modules were not run, so it judges none.
    if key.startswith("quality-unknown:") or key in ("pytest:<no identity>", "pytest:<beyond tests>"):
        return "unmeasured"
    # A HARD_ZERO metric is a correctness invariant (an omission that raises nothing at
    # runtime), never a count to relax, so it is scored like a behavior test.
    if key.startswith("quality:") and key[8:] in HARD_ZERO:
        return "behavior"
    if key.startswith(("quality:", QUALITY_JOB)):
        return "ratchet"
    if key.startswith("pytest:"):
        return "ratchet" if key[7:].startswith(RATCHET_TESTS) else "behavior"
    # `<job>: <step>`: classify the failed step only, since a job's display name can name
    # a ratchet ("reqs-check + native wheel + contract drift") while its step is behavior.
    step = key.split(": ", 1)[1] if ": " in key else key
    return "ratchet" if RATCHET_STEPS.search(step) else "behavior"


def fix_kind(files: Sequence[str]) -> str:
    """What a branch's own commits changed between a red head and the head that cleared it."""
    if not files:
        return "none"
    if any(not PAPERWORK.match(f) and not TEST_PATH.search(f) for f in files):
        return "code"
    if all(PAPERWORK.match(f) for f in files):
        return "paperwork"
    return "tests"


@dataclass
class Head:
    """One completed, non-cancelled PR run: its branch, head, time and red gate keys."""

    workflow: str
    branch: str
    sha: str
    at: dt.datetime
    red: frozenset[str]
    measured: bool = True
    # Gating jobs this run skipped (a failed `scope` skips `quality`): they passed nothing.
    skipped: frozenset[str] = frozenset()
    # Failed pytest/quality jobs whose log this run needed, and how many of those were unread.
    log_jobs: int = 0
    unread_logs: int = 0
    # The pytest modules this run executed, read from each shard's selection report: None
    # when every shard ran its full slice, a set when some ran only affected modules.
    pytest_ran: frozenset[str] | None = None
    # A Trunk queue draft, authenticated the way ci.yml's guards do it (trunk-merge/ head,
    # opened by trunk-io[bot], same-repo head), never by the branch name alone.
    draft: bool = False
    # Cancelled runs are kept, unmeasured, so a cancellation nothing superseded still counts.
    cancelled: bool = False
    # When the run was created (a rerun keeps it), so shas order by push, not by finish.
    created: dt.datetime | None = None
    # The run's id, which GitHub assigns in creation order: it breaks a tie between two shas
    # pushed within one second, whose second-resolution `created_at` values compare equal.
    run_id: int = 0


@dataclass
class GateScore:
    key: str
    cls: str
    branches: set[str] = field(default_factory=set)
    red_heads: int = 0
    queue_draft_reds: int = 0
    sole_red_heads: int = 0
    held_hours: float = 0.0
    fixes: Counter[str] = field(default_factory=Counter)
    unresolved: int = 0
    red_on_main: int = 0
    # PR heads in the window whose verdict was unmeasured: whatever reds they hid
    # and fixes are missing from every count, so no gate may be demoted beside them.
    partial_heads: int = 0

    def _settled(self) -> tuple[str, str] | None:
        """A verdict that needs no fix evidence: too quiet to judge, or never demotable."""
        if len(self.branches) < MIN_BRANCHES:
            return "QUIET", f"red on {len(self.branches)} branch(es), below {MIN_BRANCHES}"
        if self.cls == "behavior":
            return "KEEP", "tests what the product does"
        if self.cls == "unmeasured":
            return "KEEP", "a failed measurement, never evidence for demoting the rule"
        return None

    def verdict(self) -> tuple[str, str]:
        if settled := self._settled():
            return settled
        known = sum(self.fixes[k] for k in ("code", "paperwork", "tests"))
        code = self.fixes["code"] / known if known else 0.0
        paper = self.fixes["paperwork"] / known if known else 0.0
        if known >= MIN_FIXES and code >= CODE_SHARE_KEEP:
            return "KEEP", f"{self.fixes['code']}/{known} fixes changed product code (inferred catch)"
        if self.partial_heads:
            return "KEEP", (
                f"{self.partial_heads} PR head(s) in the window had no measured verdict: "
                "no demotion on partial evidence"
            )
        if self.fixes["unreadable"]:
            # Fail closed: an unreadable fix may have changed product code, so the readable
            # ones alone are never evidence for demoting the rule.
            return "KEEP", f"{self.fixes['unreadable']} fix(es) unreadable: no demotion on partial evidence"
        if known >= MIN_FIXES and paper >= PAPERWORK_SHARE_AFTER_MERGE:
            return "AFTER-MERGE", f"{self.fixes['paperwork']}/{known} fixes were paperwork only"
        if self.sole_red_heads and known < MIN_FIXES:
            # Fail closed: demoting a merge block needs readable evidence of what its fixes were.
            return "KEEP", (
                f"held {self.sole_red_heads} PR head(s) on its own, but only {known} readable "
                f"fix(es) (< {MIN_FIXES}): no evidence for demoting it"
            )
        if self.sole_red_heads:
            return "WARN", (
                f"held {self.sole_red_heads} PR head(s) on its own for {self.held_hours:.1f} h; "
                f"{known} known fix(es), {self.fixes['code']} with code"
            )
        return "KEEP", "never the only red on a head, so demoting it frees nothing alone"


def _by_branch_sha(heads: Sequence[Head]) -> dict[str, dict[str, dict[str, Head]]]:
    """branch -> sha -> workflow -> newest run. ci.yml and e2e.yml judge the same commit, so
    a head is its sha's verdict across both: red in either one is red on that head."""
    grouped: dict[str, dict[str, dict[str, Head]]] = defaultdict(lambda: defaultdict(dict))
    for head in heads:
        runs = grouped[head.branch][head.sha]
        if head.workflow not in runs or runs[head.workflow].at < head.at:
            runs[head.workflow] = head
    return grouped


def _cleared_at(
    key: str, runs: Mapping[str, Head], later: Sequence[str], shas: Mapping[str, Mapping[str, Head]]
) -> str | None:
    """The first later sha with a full verdict where a workflow that judged `key` red ran,
    was measured, ran the job the key comes from (not skipped behind a failed dependency)
    and passed it."""
    flows = [w for w, h in runs.items() if key in h.red]
    job = origin_job(key)
    for sha in later:
        # A clearing head needs a full verdict too: a pass beside a timed-out workflow is
        # partial evidence, and counting it as the fix would score a fix nobody measured.
        if full_verdict(shas[sha]) and any(_passed(shas[sha].get(w), key, job) for w in flows):
            return sha
    return None


def verdict_at(runs: Mapping[str, Head]) -> dt.datetime:
    """When a sha's verdict was complete: its last workflow's finish."""
    return max(h.at for h in runs.values())


def full_verdict(runs: Mapping[str, Head]) -> bool:
    """Both workflows run on every PR; a sha missing one (cancelled when superseded) or with
    an unmeasured run has no full verdict."""
    return set(WORKFLOWS) <= runs.keys() and all(h.measured for h in runs.values())


def _owned(runs: Mapping[str, Head], window: tuple[dt.datetime, dt.datetime] | None) -> bool:
    return window is None or window[0] <= verdict_at(runs) < window[1]


def _fix_counts(gate: GateScore, cleared: str | None, blind: bool) -> str | None:
    """Book a red with no readable fix on `gate`; the clearing sha when it is a fix to classify."""
    if blind:
        gate.fixes["unreadable"] += 1
        return None
    gate.unresolved += cleared is None
    return cleared


def _blind_fix(
    key: str, flows: Sequence[str], fixable: Sequence[str], cleared: str | None, shas: Mapping[str, Mapping[str, Head]]
) -> bool:
    """Whether a head consulted for a red's fix may be where it really cleared unseen: it
    holds an unread log (a selection report read as "ran nothing"), or it has no full
    verdict while a workflow that judged the red ran there uncancelled and did not judge it
    red again (its companion timed out or never finished). Either way the fix is unreadable,
    not unresolved. A head whose red-judging workflows were all cancelled was superseded and
    said nothing. A later measured red on `key` among them proves only the gate's state at
    that head: a partial head before it may still be where this red cleared and then regressed,
    so it stays this red's blind spot, while heads from the later red on belong to that red
    (see `_last_red_before`) and are never consulted here."""
    consulted = fixable if cleared is None else fixable[: fixable.index(cleared) + 1]
    later_red = [
        n for n, x in enumerate(consulted) if full_verdict(shas[x]) and any(key in h.red for h in shas[x].values())
    ]
    consulted = consulted[: later_red[0]] if later_red else consulted
    return any(
        any(h.unread_logs for h in shas[x].values())
        or (
            not full_verdict(shas[x])
            and any(w in shas[x] and not shas[x][w].cancelled and key not in shas[x][w].red for w in flows)
        )
        for x in consulted
    )


def _last_red_before(
    key: str, order: Sequence[str], i: int, cleared: str, shas: Mapping[str, Mapping[str, Head]]
) -> bool:
    """Whether order[i] is the last scorable red on `key` before `cleared`. The fix belongs to
    that head alone, so a red just before a window boundary and one just after it never both
    count the same clearing sha, whichever window each is scored in."""
    between = order[i + 1 : order.index(cleared)]
    return not any(full_verdict(shas[s]) and any(key in h.red for h in shas[s].values()) for s in between)


def _scored_keys(runs: Mapping[str, Head], window: tuple[dt.datetime, dt.datetime] | None) -> frozenset[str]:
    """The red keys of one head this window scores: none without a full verdict, and none
    for a head whose verdict completed in another window, so each red is scored once."""
    red: frozenset[str] = frozenset().union(*(h.red for h in runs.values()))
    if not red or not full_verdict(runs) or not _owned(runs, window):
        return frozenset()
    return red


def _passed(head: Head | None, key: str, job: str) -> bool:
    ran = (
        head is None
        or head.pytest_ran is None
        or not key.startswith("pytest:")
        or any(key[7:] == m or key[7:].startswith(m.rstrip("/") + "/") for m in head.pytest_ran)
    )
    return (
        head is not None
        and head.measured
        and ran
        and job not in head.skipped
        and key not in head.skipped
        and key not in head.red
    )


def _sole_red(runs: Mapping[str, Head], red: frozenset[str]) -> bool:
    """Both workflows run on every PR; a sha missing one (cancelled when superseded) or
    with an unmeasured run has no full verdict, so it is never sole red."""
    return full_verdict(runs) and all(gate_class(k) == "ratchet" for k in red)


def _is_draft(shas: Mapping[str, Mapping[str, Head]]) -> bool:
    return any(h.draft for runs in shas.values() for h in runs.values())


def head_summary(heads: Sequence[Head], window: tuple[dt.datetime, dt.datetime] | None = None) -> tuple[int, int]:
    """(PR heads red on a named gate, of them red on ratchets alone), one verdict per sha
    across both workflows, the same grouping and window ownership `score()` uses. Queue
    drafts are left out."""
    red_heads = ratchet_only = 0
    for shas in _by_branch_sha(heads).values():
        if _is_draft(shas):
            continue
        for runs in shas.values():
            red = frozenset().union(*(h.red for h in runs.values()))
            if red and full_verdict(runs) and _owned(runs, window):
                red_heads += 1
                ratchet_only += _sole_red(runs, red)
    return red_heads, ratchet_only


def score(
    heads: Sequence[Head],
    main_red: Mapping[str, int],
    fix_files: Callable[[str, str], Sequence[str] | None],
    window_end: dt.datetime,
    window: tuple[dt.datetime, dt.datetime] | None = None,
    horizon: dt.timedelta | None = None,
) -> dict[str, GateScore]:
    """Every gate's score over the window. `fix_files(red_sha, fix_sha)` returns the
    branch's own changed files, or None when they could not be read.

    `window` is the (start, end) a red head must have completed its verdict in to be
    scored here; `heads` reach past it on both sides, so a head just inside it whose runs
    started earlier is complete, and its fix can land after the window ends. Each red head
    is therefore scored in exactly one window. A head without a full verdict (a workflow
    missing or unmeasured) is never scored: it adds no branch, hold, transition or fix.
    `horizon` bounds how long after a red's verdict a fix may land and still count, so a
    red early in the window gets no longer to be fixed than one late in it."""
    scores: dict[str, GateScore] = {}
    partial = unmeasured_heads(heads, window)

    def get(key: str) -> GateScore:
        if key not in scores:
            scores[key] = GateScore(key, gate_class(key), red_on_main=main_red.get(key, 0), partial_heads=partial)
        return scores[key]

    # One fix per red-to-green transition: the last red head before the clearing head.
    transitions: dict[tuple[str, str], str] = {}
    for branch, shas in _by_branch_sha(heads).items():
        order = _push_order(shas)
        draft = _is_draft(shas)
        for i, sha in enumerate(order):
            runs = shas[sha]
            red = _scored_keys(runs, window)
            if not red:
                continue
            red_at = min(h.at for h in runs.values() if h.red)
            if draft:
                for key in red:
                    get(key).queue_draft_reds += 1
                continue
            later = order[i + 1 :]
            # Every red gets the same fix horizon, wherever in the window it fell.
            fixable = [s for s in later if horizon is None or verdict_at(shas[s]) <= verdict_at(runs) + horizon]
            # An unread log may hide a behavior failure, so such a head never counts as sole red.
            sole = _sole_red(runs, red)
            # The next head's verdict lands when its slowest workflow finishes, not its first.
            # A sha whose every run was cancelled gave no verdict, so the hold runs past it.
            # A partial head (a companion timed out) gave no verdict either.
            given = [x for x in later if full_verdict(shas[x])]
            next_at = max(h.at for h in shas[given[0]].values()) if given else window_end
            held = min(max((next_at - red_at).total_seconds(), 0) / 3600, HELD_CAP_H)
            for key in red:
                gate = get(key)
                gate.branches.add(branch)
                gate.red_heads += 1
                if sole:
                    gate.sole_red_heads += 1
                    gate.held_hours += held / len(red)
                cleared = _cleared_at(key, runs, fixable, shas)
                flows = [w for w, h in runs.items() if key in h.red]
                fix = _fix_counts(gate, cleared, _blind_fix(key, flows, fixable, cleared, shas))
                if fix is not None and _last_red_before(key, order, i, fix, shas):
                    transitions[(key, fix)] = sha
    for (key, cleared), sha in transitions.items():
        files = fix_files(sha, cleared)
        get(key).fixes["unreadable" if files is None else fix_kind(files)] += 1
    # A gate red only on main is still scored, so its main reds are in the table.
    list(map(get, main_red))
    return scores


def _push_order(shas: Mapping[str, Mapping[str, Head]]) -> list[str]:
    """A branch's shas oldest push first: by each sha's earliest run creation, since a
    completion time moves with a slow workflow or a rerun of an older sha."""
    return sorted(shas, key=lambda s: min((h.created or h.at, h.run_id) for h in shas[s].values()))


def _head_counts(heads: Sequence[Head], window: tuple[dt.datetime, dt.datetime] | None) -> tuple[int, int]:
    """(incomplete, unmeasured) PR shas whose verdict completed in `window`, from one walk so
    the report's count and the gates' count cannot drift apart. Incomplete is every sha
    without a full verdict (a workflow missing, or unmeasured). Unmeasured is the subset not
    superseded: a sha whose only unmeasured runs were cancelled is exempt only when a newer
    sha on its branch superseded it, since an operator or an outage can cancel a run too, and
    then its verdict is just missing. A workflow with no run at all (still queued, or never
    collected) is partial the same way on a branch's newest sha; on an older one a newer push
    superseded it. A timed-out job usually names no gate, and may hide any."""
    incomplete = unmeasured = 0
    for shas in _by_branch_sha(heads).values():
        if _is_draft(shas):
            continue
        newest = _push_order(shas)[-1]
        for sha, runs in shas.items():
            if full_verdict(runs) or not _owned(runs, window):
                continue
            incomplete += 1
            unread = [h for h in runs.values() if not h.measured]
            unmeasured += not (sha != newest and all(h.cancelled for h in unread))
    return incomplete, unmeasured


def unmeasured_heads(heads: Sequence[Head], window: tuple[dt.datetime, dt.datetime] | None) -> int:
    """PR shas in `window` with no measured verdict that no newer push superseded."""
    return _head_counts(heads, window)[1]


def render_markdown(scores: Mapping[str, GateScore], meta: Mapping[str, object]) -> str:
    rows = sorted(scores.values(), key=lambda g: (-g.held_hours, -len(g.branches), g.key))
    sole_total = sum(g.sole_red_heads for g in scores.values())
    lines = [
        f"# Ratchet scorecard {meta['date']}",
        "",
        f"Window: {meta['since']} to {meta['until']} (UTC); a fix counts when it lands within "
        f"{LOOKAHEAD.total_seconds() / 3600:g} h of its red. "
        f"{meta['heads']} PR heads finished their verdict in it, across {', '.join(WORKFLOWS)}; {meta['red_heads']} PR heads red with a named gate, "
        f"{meta['ratchet_only_heads']} of them red on ratchets alone. Heads not scored for want of a full "
        f"verdict: {meta['incomplete_heads']}. Logs unreadable: "
        f"{meta['unreadable_logs']}.",
        "",
        "Held hours: PR time between a ratchet-only red head and the branch's next verdict, "
        f"capped at {HELD_CAP_H:g} h. Fix kinds come from the branch's own commits; a `code` "
        "fix is consistent with a real catch but is inferred, not measured.",
        "",
        "| verdict | gate | class | branches | red heads | sole red | held h | fixes code/paper/tests | unresolved | queue drafts | why |",
        "|:--|:--|:--|--:|--:|--:|--:|:--|--:|--:|:--|",
    ]
    for g in rows:
        verdict, why = g.verdict()
        fixes = f"{g.fixes['code']}/{g.fixes['paperwork']}/{g.fixes['tests']}"
        lines.append(
            f"| {verdict} | `{g.key}` | {g.cls} | {len(g.branches)} | {g.red_heads} | "
            f"{g.sole_red_heads} | {g.held_hours:.1f} | {fixes} | {g.unresolved} | "
            f"{g.queue_draft_reds} | {why} |"
        )
    lines += ["", f"Sole-red ratchet heads in window: {sole_total}."]
    return "\n".join(lines) + "\n"


# ----- GitHub collection ---------------------------------------------------------------


def _gh_api(path: str) -> str:
    gh = shutil.which("gh")
    if gh is None:
        raise SystemExit("[ERROR] gh is not on PATH; the scorecard reads GitHub through `gh api`")
    proc = subprocess.run([gh, "api", path], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"gh api {path} failed: {proc.stderr.strip()[:200]}")
    return proc.stdout


def _iso(at: str) -> dt.datetime:
    return dt.datetime.fromisoformat(at.replace("Z", "+00:00"))


def list_runs(api: Api, workflow: str, start: dt.datetime) -> list[dict]:
    """Completed runs of one workflow created in one SLICE_H-hour slice from `start`. The
    listing stops at 1,000 results, which one day of ci.yml comes close to (about 750 on
    Wed 14 Oct 2026), so slices are short and a full slice is an error, never a cut."""
    end = start + dt.timedelta(hours=SLICE_H) - dt.timedelta(seconds=1)
    span = f"{start:%Y-%m-%dT%H:%M:%SZ}..{end:%Y-%m-%dT%H:%M:%SZ}"
    runs: list[dict] = []
    for page in range(1, 11):
        body = json.loads(
            api(
                f"repos/{REPO}/actions/workflows/{workflow}/runs?per_page=100&page={page}"
                f"&status=completed&created={span}"
            )
        )
        batch = body["workflow_runs"]
        runs.extend(batch)
        if len(batch) < 100:
            return runs
    raise RuntimeError(f"{workflow} in {span} has more than 1,000 runs; shorten SLICE_H")


@dataclass
class Collected:
    heads: list[Head]
    main_red: Counter[str]
    runs: int
    unreadable_logs: int
    log_jobs: int = 0
    # Unreadable logs of main pushes in the scored window: the main regressions they would
    # have counted are the evidence a demoted gate is monitored by, so they fail the window too.
    main_unread_logs: int = 0


def _head_repo(run: Mapping) -> str | None:
    """The repository a run's head lives in (None when the fork was deleted)."""
    return (run.get("head_repository") or {}).get("full_name")


def _branch_key(run: Mapping) -> str:
    """A PR head's branch identity: its name, prefixed with the fork it lives in when that is
    not this repository, so two forks' `patch-1` branches never share one sha history."""
    repo = _head_repo(run)
    if repo is None:
        # A deleted fork keeps no identity, so each of its heads is a history of its own:
        # nothing can supersede it, and a cancellation on it stays a missing verdict.
        return f"deleted-fork:{run['head_branch']}:{run['head_sha']}"
    return run["head_branch"] if repo == REPO else f"{repo}:{run['head_branch']}"


def _is_queue_draft(run: Mapping) -> bool:
    """ci.yml's own test for a Trunk queue draft: a trunk-merge/ head opened by trunk-io[bot]
    from this repository. A contributor branch that merely starts trunk-merge/ is a PR."""
    return (
        run["head_branch"].startswith("trunk-merge/") and run["actor"]["login"] == TRUNK_BOT and _head_repo(run) == REPO
    )


def runs_to_read(wanted: Sequence[tuple[str, dict]]) -> list[tuple[str, dict]]:
    """Every run whose jobs the scorecard must read: each failed run, and each successful PR
    run on a branch that has a failed PR run. A success can only clear a red on its own
    branch, and a docs-only success skips the quality job, so its jobs say what it passed.
    Every main push of ci.yml is read too: main runs the quality gate report-only, so a
    passing main run can still log a REGRESSION, and that debt is what `main_red` counts."""
    # A cancelled run is read from its conclusion alone: its jobs judged nothing.
    failed = [(w, r) for w, r in wanted if r["conclusion"] not in ("success", "skipped", "cancelled")]
    red_branches = {_branch_key(r) for _, r in failed if r["event"] == "pull_request"}
    clearing = [
        (w, r)
        for w, r in wanted
        if r["conclusion"] in ("success", "skipped") and r["event"] == "pull_request" and _branch_key(r) in red_branches
    ]
    # A failed main push is already read; a passing or cancelled one is read for its report.
    main = [(w, r) for w, r in wanted if _is_main_report(w, r) and (w, r) not in failed]
    return failed + clearing + main


def _is_main_report(workflow: str, run: Mapping) -> bool:
    """A completed main push of ci.yml, whose quality job ran report-only. Its conclusion
    does not matter: a main run red on an unrelated job still logged the quality report."""
    return workflow == "ci.yml" and run["event"] == "push" and run["conclusion"] is not None


def _in_main_window(run: Mapping, main_window: tuple[dt.datetime, dt.datetime] | None) -> bool:
    return main_window is None or main_window[0] <= _iso(run["updated_at"]) < main_window[1]


def _job_log(api: Api, job: Mapping) -> tuple[int, str | None]:
    """One job's log, or None when it cannot be read (expired, or the API refused)."""
    try:
        return job["id"], api(f"repos/{REPO}/actions/jobs/{job['id']}/logs") or None
    except RuntimeError:
        return job["id"], None


def _selection_branches(
    wanted: Sequence[tuple[str, dict]], verdicts: Mapping[int, tuple[frozenset[str], bool, int]]
) -> set[str]:
    """The PR branches where some run was red on a pytest key: only there can a shard's
    selection clear a red, so only there is its selection report consulted."""
    return {
        _branch_key(run)
        for _, run in wanted
        if run["event"] == "pull_request" and any(k.startswith("pytest:") for k in verdicts[run["id"]][0])
    }


def _selection_logs(
    api: Api,
    wanted: Sequence[tuple[str, dict]],
    jobs: Mapping[int, list[dict]],
    verdicts: Mapping[int, tuple[frozenset[str], bool, int]],
    logs: Mapping[int, str | None],
    workers: int,
) -> dict[int, str | None]:
    """Logs of the pytest shards that could clear a pytest red: every shard, passing or not,
    of a PR run on a branch where some run was red on a pytest key. A PR head runs only its
    affected modules, so a module absent from a later head's selection was never run there."""
    branches = _selection_branches(wanted, verdicts)
    shards = [
        j
        for _, run in wanted
        if run["event"] == "pull_request" and _branch_key(run) in branches
        for j in jobs.get(run["id"], [])
        if j["name"].split(" (")[0] in PYTEST_JOBS and j["conclusion"] != "skipped" and j["id"] not in logs
    ]

    with ThreadPoolExecutor(workers) as pool:
        return dict(pool.map(lambda j: _job_log(api, j), shards))


def collect(
    api: Api,
    since: dt.datetime,
    until: dt.datetime,
    workers: int = 8,
    main_window: tuple[dt.datetime, dt.datetime] | None = None,
    finished_since: dt.datetime | None = None,
) -> Collected:
    raw: list[tuple[str, dict]] = []
    start = since.replace(minute=0, second=0, hour=since.hour - since.hour % SLICE_H)
    while start <= until:
        for workflow in WORKFLOWS:
            raw += [(workflow, r) for r in list_runs(api, workflow, start)]
        start += dt.timedelta(hours=SLICE_H)
    # The listing selects by creation time, so a run created in the window can finish after
    # it; its verdict belongs to the next window, never to both.
    raw = [(w, r) for w, r in raw if since <= _iso(r["created_at"]) and _iso(r["updated_at"]) <= until]
    # The look-behind exists to catch the runs of a sha whose verdict lands in the window, so
    # a sha with nothing finishing from `finished_since` on is never read: its jobs would cost
    # API calls and could belong to no window being scored.
    if finished_since is not None:
        live = {r["head_sha"] for _, r in raw if finished_since <= _iso(r["updated_at"])}
        raw = [(w, r) for w, r in raw if r["head_sha"] in live]
    wanted = [
        (w, r)
        for w, r in raw
        if r["event"] == "pull_request"
        or (r["event"] == "push" and r["head_branch"] == "main" and r["conclusion"] != "cancelled")
    ]
    # A cancelled main push leaves no report, and its per-sha concurrency group means no
    # rerun replaces it. The quality gate measures main's whole tree against the committed
    # baseline, so a later main push that did report covers it; one with none after it is
    # a gap.
    cancelled_main = [
        r for w, r in raw if _is_main_report(w, r) and r["head_branch"] == "main" and r["conclusion"] == "cancelled"
    ]
    failed = runs_to_read(wanted)

    def jobs_of(item: tuple[str, dict]) -> tuple[int, list[dict]]:
        run = item[1]
        body = json.loads(api(f"repos/{REPO}/actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs?per_page=100"))
        return run["id"], body["jobs"]

    with ThreadPoolExecutor(workers) as pool:
        jobs = dict(pool.map(jobs_of, failed))

    main_reports = {r["id"] for w, r in wanted if _is_main_report(w, r)}
    log_jobs = [
        j
        for run_id, js in jobs.items()
        for j in js
        if (
            j["conclusion"] == "failure"
            and j["name"].split(" (")[0] not in NON_GATE_JOBS
            and j["name"].split(" (")[0] in PYTEST_JOBS | {QUALITY_JOB}
        )
        or (run_id in main_reports and _main_report_job(j))
    ]

    with ThreadPoolExecutor(workers) as pool:
        logs = dict(pool.map(lambda j: _job_log(api, j), log_jobs))

    verdicts = {run["id"]: judge_jobs(jobs.get(run["id"], []), logs) for _, run in wanted}
    selection = _selection_logs(api, wanted, jobs, verdicts, logs, workers)
    logs = {**logs, **selection}
    consulted = _selection_branches(wanted, verdicts)

    heads: list[Head] = []
    main_red: Counter[str] = Counter()
    unreadable = 0
    main_unread = 0
    log_ids = {j["id"] for j in log_jobs}
    reported: list[dt.datetime] = []
    for workflow, run in wanted:
        red, measured, missing = verdicts[run["id"]]
        measured = measured and run_gave_verdict(run["conclusion"], jobs.get(run["id"], []))
        unreadable += missing
        if run["event"] == "push":
            report_unread = sum(
                j["id"] in log_ids and _main_report_job(j) and not logs.get(j["id"]) for j in jobs.get(run["id"], [])
            )
            # Only ci.yml holds the quality job; an e2e.yml main push's jobs are never read.
            gaps = _main_report_gaps(jobs.get(run["id"], []), red) if _is_main_report(workflow, run) else 0
            if _is_main_report(workflow, run) and not (missing or report_unread or gaps):
                reported.append(_iso(run["created_at"]))
            # Main's reds are counted for the scored window only, never its look-behind.
            if _in_main_window(run, main_window):
                main_red.update(red | _main_report_keys(jobs.get(run["id"], []), logs))
                unreadable += report_unread + gaps
                main_unread += missing + report_unread + gaps
            continue
        skipped = skipped_jobs(jobs.get(run["id"], []))
        # A shard's unread or missing selection report would read as "ran no modules", so it
        # is an unread log like any other: it fails the window rather than dropping a clear.
        failed_logs = logs if _branch_key(run) in consulted else {}
        sel_missing = unread_selections(jobs.get(run["id"], []), selection, failed_logs)
        unreadable += sel_missing
        missing += sel_missing
        needed = sum(j["id"] in log_ids or j["id"] in selection for j in jobs.get(run["id"], []))
        heads.append(
            Head(
                workflow,
                _branch_key(run),
                run["head_sha"],
                _iso(run["updated_at"]),
                red,
                measured,
                skipped,
                needed,
                missing,
                run_selection(jobs.get(run["id"], []), logs),
                _is_queue_draft(run),
                run["conclusion"] == "cancelled",
                _iso(run["created_at"]),
                run["id"],
            )
        )
    uncovered = sum(
        _in_main_window(r, main_window) and not any(t > _iso(r["created_at"]) for t in reported) for r in cancelled_main
    )
    unreadable += uncovered
    main_unread += uncovered
    return Collected(heads, main_red, len(wanted), unreadable, len(log_jobs), main_unread)


def _main_report_gaps(jobs: Sequence[Mapping], red: frozenset[str]) -> int:
    """Main quality jobs that ended without a report: every quality job that was not
    cancelled and whose measuring step did not complete (success or failure), whatever
    the job's own conclusion (failure before the step, skipped, timed_out, stale), or
    whose report left a metric UNKNOWN. A run with no quality job at all is one too (a
    startup_failure or action_required run lists no jobs). Main never logs those gates,
    so scoring them would read silence as a clean main. A cancelled quality job is not
    counted, and `collect` drops cancelled main runs before this: they are the
    superseded-push case, a quarter of main runs. Only a pull_request can come out of
    scope (ci.yml `scope`), so on a main push a skipped quality job is a gap too."""
    step = MEASURING_STEPS[QUALITY_JOB]
    quality = [j for j in jobs if j["name"].split(" (")[0] == QUALITY_JOB]
    if not quality:
        return 1
    gaps = sum(
        j["conclusion"] != "cancelled"
        and not any(
            st["name"].startswith(step) and st["conclusion"] in ("success", "failure") for st in j.get("steps", [])
        )
        for j in quality
    )
    return gaps or int(any(k.startswith("quality-unknown:") for k in red))


def _main_report_job(job: Mapping) -> bool:
    """A main quality job whose log holds the report-only REGRESSION lines: any that did
    not fail (a failed one is read as a red), was not cancelled and was not skipped. That
    includes one that timed out or went stale after its report step, say in the upload."""
    return job["name"].split(" (")[0] == QUALITY_JOB and job["conclusion"] not in ("failure", "cancelled", "skipped")


def _main_report_keys(jobs: Sequence[Mapping], logs: Mapping[int, str | None]) -> frozenset[str]:
    """The REGRESSION keys a report-only quality job that did not fail logged on main."""
    return frozenset(
        k
        for j in jobs
        if _main_report_job(j) and logs.get(j["id"])
        for k in quality_keys((logs[j["id"]] or "").splitlines())
        if k.startswith("quality:")
    )


def _first_parent_chain(parents: Mapping[str, list[str]], red_sha: str, fix_sha: str) -> list[str] | None:
    """Every commit on the first-parent chain from fix back to red (merges included),
    or None if it never gets there."""
    chain: list[str] = []
    sha = fix_sha
    while sha != red_sha:
        if not parents.get(sha):
            return None
        chain.append(sha)
        sha = parents[sha][0]
    return chain


def _first_parent_own(parents: Mapping[str, list[str]], red_sha: str, fix_sha: str) -> list[str] | None:
    """Non-merge commits on the first-parent chain from fix back to red, or None if it never gets there."""
    chain = _first_parent_chain(parents, red_sha, fix_sha)
    return None if chain is None else [sha for sha in chain if len(parents[sha]) == 1]


def _compare_files(api: Api, base: str, head: str) -> set[str]:
    """Files that differ between merge-base(base, head) and head. GitHub does not page a
    compare's files, so a list at its cap raises rather than being read as complete."""
    files = {f["filename"] for f in json.loads(api(f"repos/{REPO}/compare/{base}...{head}")).get("files", [])}
    if len(files) >= MAX_COMPARE_FILES:
        raise RuntimeError(f"compare {base}...{head} lists {len(files)} files, GitHub's cap")
    return files


def _on_main(api: Api, sha: str, at: str) -> bool:
    """Whether `sha` was in main's history at `at`, the merge's commit time, so that a merge
    of it brought main in. Today's main alone is the wrong question: a sibling branch merged
    into the fix and landed on main later would read as main, and its product code would
    drop out. So the PR that introduced `sha` to main must have merged by `at`; a sha with no
    such PR (a direct push) is never assumed to be main, so its merge counts every file."""
    status = json.loads(api(f"repos/{REPO}/compare/{sha}...main?per_page=1"))["status"]
    if status not in ("ahead", "identical"):
        return False
    # For a commit on main, GitHub lists only the merged PR that introduced it to main.
    pulls = json.loads(api(f"repos/{REPO}/commits/{sha}/pulls?per_page=100"))
    landed = [_iso(p["merged_at"]) for p in pulls if p.get("merged_at")]
    return bool(landed) and min(landed) <= _iso(at)


def _merge_files(api: Api, merge_sha: str, branch_parent: str, other_parent: str, at: str, per_page: int) -> set[str]:
    """The files a merge on the branch's own chain counts as its work. A merge of main counts
    only what it could have resolved or hand-edited; a merge of any other branch counts every
    file it brought, since that branch's commits may be the fix itself."""
    if _on_main(api, other_parent, at):
        return _merge_candidates(api, merge_sha, branch_parent, other_parent, per_page)
    return _compare_files(api, branch_parent, merge_sha)


def _merge_candidates(api: Api, merge_sha: str, branch_parent: str, main_parent: str, per_page: int) -> set[str]:
    """Every file a merge could have resolved or hand-edited, counted as the branch's own work.

    A conflict, however it was resolved (ours, theirs or by hand), can only sit in a file both
    sides changed since their merge base. An edit made in the merge itself shows as a file the
    merge left different from one parent although the other parent never changed it. Both
    sets together are a superset of what the merge authored, so the fix errs toward `code`.
    """
    branch_side = _compare_files(api, main_parent, branch_parent)
    main_side = _compare_files(api, branch_parent, main_parent)
    vs_branch = set(_commit_files(api, merge_sha, per_page))  # GitHub diffs a merge against its first parent
    vs_main = _compare_files(api, main_parent, merge_sha)
    return (branch_side & main_side) | (vs_branch - main_side) | (vs_main - branch_side)


def _commit_files(api: Api, sha: str, per_page: int) -> list[str]:
    """Every file one commit changed. GitHub pages a commit's file list, so read every page;
    a page that cannot be read raises, leaving the fix unreadable rather than half-classified."""
    names: list[str] = []
    for page in range(1, MAX_FILE_PAGES + 1):
        files = json.loads(api(f"repos/{REPO}/commits/{sha}?per_page={per_page}&page={page}")).get("files", [])
        names.extend(f["filename"] for f in files)
        if len(files) < per_page:
            return names
    raise RuntimeError(f"commit {sha} lists more files than GitHub serves ({MAX_FILE_PAGES} pages)")


def own_files(api: Api, per_page: int = 100) -> Callable[[str, str], Sequence[str] | None]:
    """Files a branch's own (non-merge) commits changed between two of its heads.

    Only the first-parent chain from `fix_sha` back to `red_sha` is the branch's own work;
    commits a merge of main brought in sit behind a second parent and are left out, but every
    file the merge could have resolved or edited is counted as the branch's own. A
    chain that never reaches `red_sha` (a rebase or force-push) is unreadable, never a guess.
    """
    cache: dict[tuple[str, str], Sequence[str] | None] = {}

    def files(red_sha: str, fix_sha: str) -> Sequence[str] | None:
        if (red_sha, fix_sha) in cache:
            return cache[(red_sha, fix_sha)]
        try:
            compare = json.loads(api(f"repos/{REPO}/compare/{red_sha}...{fix_sha}"))
            parents = {c["sha"]: [p["sha"] for p in c["parents"]] for c in compare["commits"]}
            # A merge whose time cannot be read raises KeyError below: the fix is unreadable.
            dates = {c["sha"]: c["commit"]["committer"]["date"] for c in compare["commits"] if "commit" in c}
            chain = _first_parent_chain(parents, red_sha, fix_sha)
            if chain is None:
                cache[(red_sha, fix_sha)] = None
                return None
            changed: set[str] = set()
            for commit_sha in chain:
                if len(parents[commit_sha]) == 1:
                    changed.update(_commit_files(api, commit_sha, per_page))
                else:
                    branch_parent, other_parent = parents[commit_sha][:2]
                    at = dates[commit_sha]
                    changed.update(_merge_files(api, commit_sha, branch_parent, other_parent, at, per_page))
            result: Sequence[str] | None = sorted(changed)
        except (RuntimeError, KeyError, json.JSONDecodeError):
            result = None
        cache[(red_sha, fix_sha)] = result
        return result

    return files


def owned_shas(heads: Sequence[Head], window: tuple[dt.datetime, dt.datetime] | None) -> list[Mapping[str, Head]]:
    """Every sha's runs with a full verdict that completed in `window`: the heads `score()`
    can score, and so the ones the report's totals count."""
    return [
        runs
        for shas in _by_branch_sha(heads).values()
        if not _is_draft(shas)
        for runs in shas.values()
        if full_verdict(runs) and _owned(runs, window)
    ]


def incomplete_heads(heads: Sequence[Head], window: tuple[dt.datetime, dt.datetime] | None) -> int:
    """PR shas whose last run finished in `window` without a full verdict: not scored, so
    counted in the report."""
    return _head_counts(heads, window)[0]


def owned_unread_logs(heads: Sequence[Head], window: tuple[dt.datetime, dt.datetime] | None) -> tuple[int, int]:
    """(unread, needed) failed pytest/quality job logs on the PR heads whose verdict completed
    in `window`, so a look-ahead run cannot make yesterday's window unmeasured."""
    owned = [
        h
        for shas in _by_branch_sha(heads).values()
        for runs in shas.values()
        if _owned(runs, window)
        for h in runs.values()
    ]
    return sum(h.unread_logs for h in owned), sum(h.log_jobs for h in owned)


def unmeasured_window(data: Collected, window: tuple[dt.datetime, dt.datetime] | None = None) -> str | None:
    """Why a window cannot be scored, or None. A window with no sha holding a measured
    verdict from every workflow (startup failures, timed-out gating jobs, every e2e run
    cancelled) scores nothing, so it is an outage, never an empty scorecard. So is a window
    with any unreadable failed-job log."""
    if not owned_shas(data.heads, window):
        return "no PR head in the window has a measured verdict from every workflow"
    # Any unreadable log fails the window: the reds it hides, and their fixes, would leave
    # every denominator, so a tier could rest on whichever evidence happened to be readable.
    unread, needed = owned_unread_logs(data.heads, window)
    if unread:
        return f"{unread} of {needed} failed pytest/quality job logs in the window were unreadable"
    if data.main_unread_logs:
        return f"{data.main_unread_logs} main pytest/quality job log(s) in the window were unreadable"
    return None


def scored_window(now: dt.datetime, days: float) -> tuple[dt.datetime, dt.datetime]:
    """The window a run started at `now` scores: it ends at the UTC midnight at least
    LOOKAHEAD back, so runs started any time on one day score the same window, and
    consecutive days tile without a gap or an overlap whatever time the scheduler fires."""
    end = (now - LOOKAHEAD).replace(hour=0, minute=0, second=0, microsecond=0)
    return end - dt.timedelta(days=days), end


def main(argv: Sequence[str] | None = None, api: Api = _gh_api) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    # One day: the ADR's seven-day baseline took 53 minutes and the whole hourly API budget.
    parser.add_argument("--days", type=float, default=DEFAULT_DAYS)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--markdown", type=Path)
    args = parser.parse_args(argv)
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    window = scored_window(now, args.days)
    since, until = window
    data = collect(api, since - LOOKBEHIND, now, main_window=window, finished_since=since)
    if reason := unmeasured_window(data, window):
        print(f"[ratchet-scorecard] UNKNOWN: {reason} between {since} and {until}")
        return 3
    scores = score(data.heads, data.main_red, own_files(api), now, window=window, horizon=LOOKAHEAD)
    red_heads, ratchet_only_heads = head_summary(data.heads, window)
    meta = {
        "date": until.date().isoformat(),
        "since": since.isoformat(),
        "until": until.isoformat(),
        "fixes_read_until": now.isoformat(),
        "heads": len(owned_shas(data.heads, window)),
        "red_heads": red_heads,
        "ratchet_only_heads": ratchet_only_heads,
        "unreadable_logs": owned_unread_logs(data.heads, window)[0] + data.main_unread_logs,
        "incomplete_heads": incomplete_heads(data.heads, window),
    }
    markdown = render_markdown(scores, meta)
    payload = {
        "meta": meta,
        "gates": [
            {
                "key": g.key,
                "class": g.cls,
                "verdict": g.verdict()[0],
                "why": g.verdict()[1],
                "branches": len(g.branches),
                "red_heads": g.red_heads,
                "sole_red_heads": g.sole_red_heads,
                "held_hours": round(g.held_hours, 2),
                "fixes": dict(g.fixes),
                "unresolved": g.unresolved,
                "queue_draft_reds": g.queue_draft_reds,
                "red_on_main": g.red_on_main,
            }
            for g in scores.values()
        ],
    }
    if args.json:
        args.json.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    if args.markdown:
        args.markdown.write_text(markdown, encoding="utf-8", newline="\n")
    sys.stdout.write(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
