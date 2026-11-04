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
    sole red      a PR head whose ONLY named failures are ratchets, and whose own aggregate
                  did not fail for a gating job it skipped. That PR was held back by rules
                  alone, so a warn tier would have let it into the queue; a head with a
                  withheld family had a second blocker, so clearing its ratchets frees nothing.
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
    UNKNOWN        an unreadable PR log sits on a head whose branch was red on this gate, or
                   in a failed job whose name puts this gate in its family, or a pytest/quality
                   job ended without a verdict (timed_out, cancelled, stale), so the gate's
                   counts are a lower bound: it is neither demoted nor held. The gates that
                   head cannot have moved are scored as usual.

"Caught a defect" is INFERRED from what the fix changed, never measured: a code change
after a red is consistent with a real catch and also with a refactor done to satisfy a
count. The scorecard says so in its output.

Exit codes: 0 measured; 3 UNKNOWN (no completed run in the window, or an unreadable MAIN log,
whose red count no PR head can stand in for). An unreadable PR log no longer fails the window:
it narrows to the gates its own branch was red on or a lost log's job can name. A gate is
never scored from an unreadable log.

Usage:
    python -m scripts.ratchet_scorecard --days 7 --json out.json --markdown out.md
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import re
import shutil
import subprocess
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Literal, TypeAlias

from scripts.ci_failure_ids import failed_identities, failure_beyond_tests
from scripts.gh_version_guard import MIN_GH_VERSION, GhVersionError, parse_gh_version
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
        # e2e.yml's advisory Electron shell job (ELECTRON-6208): continue-on-error and out of
        # `e2e verdict`'s needs until the owner swaps it for the WebKit steps.
        "e2e electron shell",
    }
)
PYTEST_JOBS = frozenset({"pytest fast lane"})
QUALITY_JOB = "quality ratchet"
# The gating jobs each workflow declares, by the base of their display name. A sha with no
# run of a workflow at all, or a run of it that collected no job, left every gate that
# workflow carries unmeasured, and an absent log can name none of them. `ci gate`, the
# quarantine list and the telemetry jobs are already excluded by NON_GATE_JOBS.
WORKFLOW_GATE_JOBS = {
    "ci.yml": frozenset(
        {
            "ci scope",
            *PYTEST_JOBS,
            "reqs-check + native wheel + contract drift",
            "production frontend build",
            "frontend unit + check + build",
            "import contracts",
            QUALITY_JOB,
        }
    ),
    "e2e.yml": frozenset({"e2e scope", "e2e gate", "e2e extended"}),
}
# The jobs that restate a workflow's verdict. One failing while no gating job did is a
# verdict nothing measured (its own checkout, its evaluator), never a green head.
AGGREGATE_JOBS = frozenset({"ci gate", "e2e verdict"})
# An aggregate's conclusion is three-valued, never a boolean. Only an explicit SUCCESS proves
# a gating job its run skipped was out of scope; a skipped, cancelled or missing aggregate
# proves nothing either way, so its run's skips stay withheld evidence.
AGGREGATE_SUCCESS = "success"
AGGREGATE_FAILURE = "failure"
AGGREGATE_UNKNOWN = "unknown"
# The step whose log mints a log-read job's keys, by ci.yml step-name prefix. These two jobs
# and every other gating job are the ADR's taxonomy table: a log-read job mints its whole
# family from that one step, so every other step of it is ancillary (checkout, setup, cache,
# ledger, upload) and mints no key; any other gating job mints one `<job>: <step>` key per
# failed step, so every step of it is a measuring step. A step this file cannot place is
# measuring, because over-scoping reads a gate UNKNOWN while under-scoping invents a green.
MEASURING_STEPS = {"pytest fast lane": "Run fast pytest lane shard", QUALITY_JOB: "Quality ratchet"}
# The other half of that table: the steps of a gating job that judge nothing, because they
# prepare the runner, move a cache/ledger/artifact, restate a verdict or clean up afterwards.
# A step of any other gating job is measuring unless it matches here: the default has to be
# measuring, because a step assumed ancillary that did judge a gate scopes nothing and the
# gate is then scored from other branches as if this head had cleared it (the P1 at
# `_skipped_gate_scope` :834), while the reverse only reads a gate UNKNOWN. A ratchet step is
# never ancillary (see the ADR table; a test holds every `RATCHET_STEPS` name out of this).
ANCILLARY_STEP = re.compile(
    r"^(?:"
    r"Reclaim |Remove untracked |Stale-install guard|actions/|Setup |Provision |Runner preflight"
    r"|System deps|libclang for bindgen|Install |Enable |Keep the |Warm the |Purge and warm the "
    r"|Cache |Restore |Download |Upload |Stage |Acquire |Resolve |Fetch |Report |Point sccache"
    r"|Stamp |Post |Virtual audio sink|Derive the worktree-port lane"
    r"|Wipe the quality gate scratch dir|Is a Trunk API token|Seam |PR test selection mode"
    r")"
)
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
    r"|apps/webui/frontend/scripts/bundle-budget\.mjs|apps/webui/frontend/budgets/"
    r"|\.importlinter|scripts/oss_tip_rules\.py"
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
    taken says nothing about the rule, so it is never scored as the ratchet.

    Whitespace after the `[quality]` tag is `\\s+`, not one space: a zero-width sequence the
    stripper correctly removes (an SGR whose parameter bytes carry an executed C0, say) leaves
    the marker looking like `[quality]  REGRESSION` on the terminal, and a single-space anchor
    would score that displayed regression as no regression at all.
    """
    keys: set[str] = set()
    for line in lines:
        match = re.search(r"\[quality\]\s+(REGRESSION|UNKNOWN):?\s+([A-Za-z][A-Za-z0-9_.\-]*)", line)
        if match:
            prefix = "quality:" if match.group(1) == "REGRESSION" else "quality-unknown:"
            keys.add(prefix + match.group(2).rstrip(":"))
    return sorted(keys)


# A pytest identity is a repository path. Controls are not path bytes, so a name that only
# matches because `\S` accepts an ESC is not a module: it is a log line to be ignored.
_PATH_BYTES = r"[^\s\x00-\x1f\x7f-\x9f]"


def pytest_keys(identities: Iterable[str]) -> list[str]:
    """`pytest:<module>` for every failing pytest identity; node ids collapse to modules."""
    keys = set()
    for identity in identities:
        match = re.match(rf"(?:FAILED|ERROR) (tests/{_PATH_BYTES}+?\.py)", identity)
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


def run_aggregate_verdict(jobs: Sequence[Mapping]) -> str:
    """The run's aggregate verdict: `AGGREGATE_SUCCESS`, `AGGREGATE_FAILURE` or `AGGREGATE_UNKNOWN`.

    `ci gate` concludes SUCCESS only when the scope job said out of scope and every gated job
    was skipped, or said in scope and every gated job succeeded. A gating job this run skipped
    is therefore exactly what a failed aggregate can name, whether it was skipped in scope or
    behind a failed dependency. The aggregate is never a gate itself (NON_GATE_JOBS), so its
    failure is only ever a statement about the jobs it aggregates.

    Skipped, cancelled and absent are one verdict here: unknown. Reading any of them as "not
    failed" would treat a skip as the out-of-scope decision on no evidence at all (`ci gate`
    can itself be skipped, and a run can end before it concludes), which clears a family the
    run never judged. Only the explicit SUCCESS proves the skip was deliberate.
    """
    conclusions = [j["conclusion"] for j in jobs if j["name"].split(" (")[0] in AGGREGATE_JOBS]
    if AGGREGATE_FAILURE in conclusions:
        return AGGREGATE_FAILURE
    if conclusions and all(c == "success" for c in conclusions):
        return AGGREGATE_SUCCESS
    return AGGREGATE_UNKNOWN


def judge_jobs(
    jobs: Sequence[Mapping], logs: Mapping[int, str | None]
) -> tuple[frozenset[str], bool, int, frozenset[str]]:
    """One run's verdict from its jobs: (red gate keys, measured, unread logs, the names of
    the failed jobs whose evidence is missing). A gating job that ended without a verdict
    (timed_out, cancelled, startup_failure), an unread log, or a quality measurement that
    could not be taken leaves the run unmeasured, so it can neither be sole red nor clear an
    earlier red. A missing verdict still leaves its job's name, which is what tells the
    scorecard which gates that missing evidence could have moved."""
    red: set[str] = set()
    measured = True
    missing = 0
    unread: set[str] = set()
    aggregate = run_aggregate_verdict(jobs)
    for job in jobs:
        base = job["name"].split(" (")[0]
        if job["conclusion"] in ("success", "skipped", "neutral") or base in NON_GATE_JOBS:
            continue
        if job["conclusion"] != "failure":
            measured = False
            # A job killed before it could conclude printed no verdict at all: its evidence is
            # as missing as a lost log, so keep its name for the same reason. The name scopes
            # only the gates that job can name (see _job_covers): a whole family for pytest and
            # the quality ratchet, its own `<job>: <step>` keys for any other gating job.
            unread.add(base)
            continue
        failed_steps = [s["name"] for s in job.get("steps", []) if s["conclusion"] == "failure"]
        needs_log = base in PYTEST_JOBS or base == QUALITY_JOB
        log = logs.get(job["id"]) if needs_log else None
        if needs_log and log is None:
            missing += 1
            measured = False
            unread.add(base)
            continue
        keys = job_keys(job["name"], failed_steps, log)
        red.update(keys)
        if any(gate_class(k) == "unmeasured" for k in keys):
            # The key names no rule, only that this job could not be measured (a quality log
            # with no REGRESSION/UNKNOWN line, a shard killed before naming a test). Keep the
            # job's name beside it, or the family it could have failed is scoped by nothing
            # and its gate is scored from other branches as if this head had cleared it.
            unread.add(base)
    if any(gate_class(k) == "unmeasured" for k in red) or (aggregate == AGGREGATE_FAILURE and not red and measured):
        measured = False
    return frozenset(red), measured, missing, frozenset(unread)


def declared_step_guards(workflow_text: str) -> dict[str, dict[str, bool]] | None:
    """Which steps of each job in a workflow file declare their own `if:`, by job base name.

    `True` means the step carries a condition of its own, so its `skipped` conclusion can be
    its own reason (`always()`, `failure()`, a matrix or event condition) rather than the
    shadow of an earlier failure. `None` means the file could not be read or parsed: nothing
    is proven, and every caller must fail closed on it.

    A step this parse cannot name (a `uses:` step with no `name:`, a step missing from the
    file a run actually executed) is simply absent, which reads as unproven, never as guarded.
    """
    try:
        import yaml
    except ImportError:  # pragma: no cover - PyYAML is a project dependency
        return None
    try:
        doc = yaml.safe_load(workflow_text)
    except yaml.YAMLError:
        return None
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    if not isinstance(jobs, dict):
        return None
    out: dict[str, dict[str, bool]] = {}
    for key, job in jobs.items():
        if not isinstance(job, dict):
            continue
        base = str(job.get("name") or key).split(" (")[0]
        steps = out.setdefault(base, {})
        for step in job.get("steps") or []:
            if not isinstance(step, dict) or not step.get("name"):
                continue
            name = str(step["name"])
            steps[name] = steps.get(name, False) or "if" in step
    return out


def workflow_step_guards(api: Api, workflow: str, sha: str) -> dict[str, dict[str, bool]] | None:
    """`declared_step_guards` for a workflow file as it stood at `sha`, or None unproven."""
    try:
        body = _api_json(api, f"repos/{REPO}/contents/.github/workflows/{workflow}?ref={sha}")
        text = base64.b64decode(body["content"]).decode("utf-8", "replace")
    except (RuntimeError, KeyError, TypeError, ValueError):
        return None
    return declared_step_guards(text)


def _skips_after_a_failure(job: Mapping) -> bool:
    """Does this job skip a step, after a step that failed, that could withhold a gate?

    Only that shape needs the workflow declaration read for it: the read decides whether such a
    skip was the failure's shadow, and order alone says nothing about the cause. A skip that
    withholds nothing — an ancillary step (an upload, a ledger download) or any skip of a job
    that gates nothing — is not read for: it cannot move `_sole_red()` or scope a gate, so a
    failed read there must not cost the run its measured verdict.
    """
    base = job["name"].split(" (")[0]
    if base in NON_GATE_JOBS:
        return False
    failed = False
    for step in job.get("steps", []):
        if step["conclusion"] == "failure":
            failed = True
        elif failed and step["conclusion"] == "skipped" and skipped_scope(f"{base}: {step['name']}"):
            return True
    return False


def _gate_skips(
    job: Mapping, guards: Mapping[str, Mapping[str, bool]] | None = None
) -> tuple[set[str], set[str]]:
    """(this job's `<job>: <step>` skips, those the workflow proves a failure caused).

    GitHub runs a job's later steps only on `success()`, so after a failed step a step with no
    condition of its own concludes `skipped`: that skip is the failure's shadow, not an
    independent one. A skip before the job's first failure (its own `if:`, a cancelled
    dependency) is independent — and so is any skip `guards` does not positively prove
    unguarded. The step payload carries no `if:`, so the proof comes from the workflow
    declarations the run's merge kept (`guard_proof`); a run whose file, job or step cannot be
    placed in them keeps its skip as independent withheld evidence, because over-scoping
    withholds a ratchet while under-scoping invents a green.
    """
    base = job["name"].split(" (")[0]
    guarded = guards.get(base, {}) if guards is not None else {}
    skipped: set[str] = set()
    behind: set[str] = set()
    failed = False
    for st in job.get("steps", []):
        if st["conclusion"] == "failure":
            failed = True
        elif st["conclusion"] == "skipped":
            key = f"{base}: {st['name']}"
            skipped.add(key)
            if failed and guarded.get(st["name"]) is False:
                behind.add(key)
    return skipped, behind


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
        out |= _gate_skips(j)[0]
    return frozenset(out)


def skips_behind_a_failure(
    jobs: Sequence[Mapping], guards: Mapping[str, Mapping[str, bool]] | None = None
) -> frozenset[str]:
    """The entries of `skipped_jobs` that the run's workflow proves a failure caused.

    The same failure that named the job's red also skipped these steps, so clearing that red
    re-runs them: they are not evidence withheld independently of the named failure, and
    `_sole_red` must not read them as a second family holding the PR back. The skips
    themselves still count everywhere else, because the gate behind such a step went
    unjudged by this head either way.

    `guards` is `declared_step_guards` for the workflow at the run's head sha. Without it
    (an unreadable file, a step the file does not name) nothing is attributed to the failure:
    order alone cannot tell a shadowed step from one whose own `if:` was false, and guessing
    would let a head read as ratchet-only while a gate it never ran stayed unmeasured.
    """
    out: set[str] = set()
    for j in jobs:
        if j["name"].split(" (")[0] in NON_GATE_JOBS or j["conclusion"] == "skipped":
            continue
        out |= _gate_skips(j, guards)[1]
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
    # GitHub's run state is retained for an incomplete head, so the report can distinguish an
    # active run from a cancelled run and from a run whose jobs never appeared.
    status: str | None = None
    conclusion: str | None = None
    # The failed pytest/quality jobs whose log this run needed and could not read: their names
    # are all that survives to say which gate families the lost evidence could have moved.
    unread_jobs: frozenset[str] = frozenset()
    # True for a run that collected no job at all: nothing inside it can be named, so every
    # gate family its workflow carries is unmeasured for the head.
    zero_jobs: bool = False
    # The run's aggregate verdict (AGGREGATE_SUCCESS / FAILURE / UNKNOWN). A failure says an
    # in-scope gating job ended without success, so a job this run skipped withheld a verdict
    # even when another job named a red. Only an explicit SUCCESS clears a skip: unknown
    # (aggregate skipped, cancelled, or never concluded) withholds it too.
    aggregate: str = AGGREGATE_UNKNOWN
    # True when this run needed a workflow-definition read to tell a shadowed skip from an
    # independent one and the file at the executed revision could not be read. Its skip
    # evidence is unproven, so the run is unmeasured rather than silently read as independent.
    guard_unread: bool = False
    # The entries of `skipped` a failure earlier in the same job caused (GitHub runs a job's
    # later steps only on `success()`). The gate behind such a step went unjudged, but the
    # skip is the named red's shadow: clearing that red re-runs the step, so `_sole_red`
    # does not read it as a second, independent family holding the PR back.
    skipped_behind_failure: frozenset[str] = frozenset()


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
    # PR heads in the window whose verdict was unmeasured and whose branch could change
    # this gate's verdict: whatever reds they hid and fixes are missing from its counts,
    # so this gate reads UNKNOWN rather than a verdict built on partial evidence.
    partial_heads: int = 0
    # Unread PR heads (not superseded) whose branch carries a red on this gate. Sibling to
    # `partial_heads`: the same heads, counted only where their absence could move a gate.
    blind_heads: int = 0

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
        if self.blind_heads and self.cls not in ("behavior", "unmeasured"):
            # A red this head hid could have landed on this gate, and the branch and fix it
            # would add are missing from the denominators, so neither demoting nor holding,
            # nor calling it too quiet to judge, is measurable.
            return "UNKNOWN", (
                f"{self.blind_heads} unread PR head(s) in the window could have moved "
                "this gate: its counts are incomplete"
            )
        if settled := self._settled():
            return settled
        known = sum(self.fixes[k] for k in ("code", "paperwork", "tests"))
        code = self.fixes["code"] / known if known else 0.0
        paper = self.fixes["paperwork"] / known if known else 0.0
        if known >= MIN_FIXES and code >= CODE_SHARE_KEEP:
            return "KEEP", f"{self.fixes['code']}/{known} fixes changed product code (inferred catch)"
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
    with an unmeasured run has no full verdict, so it is never sole red. Neither is a head
    whose aggregate failed for a gating job it skipped: clearing every ratchet it named would
    not have freed the PR, because the aggregate was independently red for the job that never
    judged it. A step that job's own earlier failure skipped is not that independent family —
    the same red skipped it, so fixing the red re-runs it (`skipped_behind_failure`)."""
    if any(_skipped_gate_scope(h, independent_only=True) for h in runs.values()):
        return False
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


def _new_gate(key: str, main_red: Mapping[str, int], blind_heads: int) -> GateScore:
    """One gate's table row, scoped to the unread heads that could have moved it."""
    return GateScore(key, gate_class(key), red_on_main=main_red.get(key, 0), blind_heads=blind_heads)


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
    scope = unread_scopes(heads, window)
    blind = unread_gate_scope(heads, window)
    scores: dict[str, GateScore] = {key: _new_gate(key, main_red, blind(key)) for key in scope}

    def get(key: str) -> GateScore:
        if key not in scores:
            scores[key] = _new_gate(key, main_red, blind(key))
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
        incomplete += sum(1 for runs in shas.values() if not full_verdict(runs) and _owned(runs, window))
        unmeasured += len(_unread_shas(shas, window))
    return incomplete, unmeasured


def _unread_shas(shas: Mapping[str, Mapping[str, Head]], window: tuple[dt.datetime, dt.datetime] | None) -> list[str]:
    """The shas on one branch whose verdict is missing and was not superseded, i.e. the
    heads whose absent reds and fixes nothing else in this window can stand in for."""
    if _is_draft(shas):
        return []
    newest = _push_order(shas)[-1]
    unread: list[str] = []
    for sha, runs in shas.items():
        if full_verdict(runs) or not _owned(runs, window):
            continue
        missed = [h for h in runs.values() if not h.measured]
        if sha != newest and all(h.cancelled for h in missed):
            continue
        unread.append(sha)
    return unread


def unread_scopes(heads: Sequence[Head], window: tuple[dt.datetime, dt.datetime] | None) -> dict[str, int]:
    """gate key -> the unread PR heads whose branch was red on that gate in the window.

    A hidden red on head H can move any gate's counts on H in either direction, so H drops
    out of every count. It can only be *proved* to move a gate that was red somewhere on its
    branch: a gate no head on that branch ever failed has no denominator entry to lose, and a
    gate the branch never touched cannot be one H made sole. Every other gate stays scoped to
    the reds it actually carries."""
    scope: dict[str, int] = {}
    for shas in _by_branch_sha(heads).values():
        unread = _unread_shas(shas, window)
        if not unread:
            continue
        # A red the branch carried only outside the window is another day's evidence: scoping
        # this gate here would read UNKNOWN on a red no head in this window can be scored on.
        keys = {key for runs in shas.values() if _owned(runs, window) for h in runs.values() for key in h.red}
        for key in keys:
            scope[key] = scope.get(key, 0) + len(unread)
    return scope


HEAD_REASON_ORDER: tuple[str, ...] = (
    "still_running",
    "cancelled",
    "missing_job",
    "missing_workflow",
    "superseded",
)


def unread_head_reasons(
    heads: Sequence[Head], window: tuple[dt.datetime, dt.datetime] | None
) -> tuple[dict[str, int], dict[str, dict[str, object]]]:
    """Explain incomplete heads, including cancellations excluded as superseded.

    An incomplete head with a missing log is reported by ``unread_log_reasons``. This separate
    inventory covers the other path to UNKNOWN: an active/cancelled run, a run with no jobs, or
    a workflow absent from the collected runs. A cancelled older sha superseded by a newer push
    is recorded as ``out_of_scope`` because ``_unread_shas`` deliberately excludes it from gate
    uncertainty; all other entries are ``UNKNOWN`` evidence.
    """
    entries: list[dict[str, object]] = []
    for shas in _by_branch_sha(heads).values():
        newest = _push_order(shas)[-1]
        for sha, runs in shas.items():
            if full_verdict(runs) or not _owned(runs, window):
                continue
            missing_workflows = set(WORKFLOWS) - set(runs)
            superseded = sha != newest and all(h.cancelled for h in runs.values() if not h.measured)
            if superseded:
                sample = next(iter(runs.values()))
                entries.append(
                    {
                        "reason": "superseded",
                        "disposition": "out_of_scope",
                        "run_id": sample.run_id,
                        "job_id": None,
                        "status": sample.status,
                        "detail": f"sha {sha[:12]} was superseded by {newest[:12]}",
                    }
                )
                continue
            if missing_workflows:
                entries.extend(
                    {
                        "reason": "missing_workflow",
                        "disposition": "UNKNOWN",
                        "run_id": None,
                        "job_id": None,
                        "status": "missing",
                        "detail": f"no {workflow} run for sha {sha[:12]}",
                    }
                    for workflow in sorted(missing_workflows)
                )
            for sample in runs.values():
                if sample.measured:
                    continue
                if sample.cancelled:
                    reason = "cancelled"
                    detail = "run was cancelled before a full verdict"
                elif sample.zero_jobs:
                    reason = "missing_job"
                    detail = "run returned no jobs"
                elif sample.status in {"queued", "in_progress", "requested", "waiting", "pending"} or sample.conclusion is None:
                    reason = "still_running"
                    detail = f"status={sample.status or 'unknown'} conclusion={sample.conclusion or 'none'}"
                else:
                    reason = "missing_job"
                    detail = f"conclusion={sample.conclusion or 'none'} did not produce a full verdict"
                entries.append(
                    {
                        "reason": reason,
                        "disposition": "UNKNOWN",
                        "run_id": sample.run_id,
                        "job_id": None,
                        "status": sample.status,
                        "detail": detail,
                    }
                )
    counts = Counter(str(entry["reason"]) for entry in entries)
    ordered_counts = {reason: counts[reason] for reason in HEAD_REASON_ORDER if counts.get(reason)}
    samples: dict[str, dict[str, object]] = {}
    for entry in entries:
        samples.setdefault(str(entry["reason"]), dict(entry))
    return ordered_counts, {reason: samples[reason] for reason in HEAD_REASON_ORDER if reason in samples}


def unmeasured_heads(heads: Sequence[Head], window: tuple[dt.datetime, dt.datetime] | None) -> int:
    """PR shas in `window` with no measured verdict that no newer push superseded."""
    return _head_counts(heads, window)[1]


def _job_covers(job: str, key: str) -> bool:
    """Whether a failed job with this base name could be where `key` was red.

    A gate key is minted from the log a job lost: the quality job names one metric at a time,
    a pytest shard one module at a time. An unread log of either is therefore silent about
    every key in its family, and naming the job is the most the log leaves behind.
    """
    if job == QUALITY_JOB:
        return key.startswith(("quality:", "quality-unknown:"))
    if job in PYTEST_JOBS:
        return key.startswith("pytest:")
    return key == job or key.startswith(job + ": ")


def skipped_scope(name: str) -> frozenset[str]:
    """The gates one entry of `Head.skipped` withheld evidence for.

    A skip is recorded as a gating job's base name or as its `<job>: <step>` key, and which
    keys it withholds follows from how that job mints them (the ADR's taxonomy table). A
    log-read job's whole family comes from the log of its measuring step, so a skip of that
    step withholds the family while an ancillary skip beside a measuring step that ran (a
    durations ledger, an upload, a cache) withholds nothing. Any other gating job mints one
    key per failed step, so a step skip withholds exactly that key, and a whole-job skip the
    family: never one step's name standing in for every key the job could have named.
    """
    base, sep, step = name.partition(": ")
    if not sep:
        return frozenset({base})
    if base in MEASURING_STEPS:
        return frozenset({base}) if step.startswith(MEASURING_STEPS[base]) else frozenset()
    return frozenset() if ANCILLARY_STEP.match(step) else frozenset({name})


def _skipped_gate_scope(h: Head, *, independent_only: bool = False) -> frozenset[str]:
    """The gate families this run's skipped gating jobs withheld evidence for.

    A skipped gating job judged nothing, so what it could have named is scoped whenever the
    run's own evidence says it should have run. An unmeasured run says so by definition. A
    measured run's skip is normally the out-of-scope decision (a docs-only change skips the
    quality job and `ci gate` still succeeds), which scopes nothing — but only that explicit
    SUCCESS says so: a failed aggregate is the run saying an in-scope gating job did not pass
    (`.github/workflows/ci.yml`: `ci gate` succeeds only when the change was out of scope or
    every gated job succeeded), and an unknown one (aggregate skipped, cancelled or never
    concluded) proves nothing at all, so the skip withheld a verdict even though another job
    named a red. What each skip withheld is `skipped_scope`'s question, not this one's.

    `independent_only` drops the skips a failure in the same job caused: the gate behind such
    a step is still unjudged, but the skip is not independent of the red that failure named.
    That is the question `_sole_red` asks; every other caller scopes both.
    """
    if h.measured and h.aggregate == AGGREGATE_SUCCESS:
        return frozenset()
    # Nothing measured says which of these skips could have carried a red, or (with a failed
    # aggregate) an in-scope job was not passed: every skip is missing evidence, whole job or
    # step, scoped by the keys its own job mints.
    names = h.skipped - h.skipped_behind_failure if independent_only else h.skipped
    return frozenset().union(*(skipped_scope(name) for name in names))


def unread_gate_scope(heads: Sequence[Head], window: tuple[dt.datetime, dt.datetime] | None) -> Callable[[str], int]:
    """gate key -> the unread PR heads in `window` whose evidence could have moved it.

    A hidden red on head H can move any gate's counts on H in either direction, so H drops
    out of every count. It is scoped to the gates it can be *proved* to have touched: the keys
    its branch was red on anywhere in the window, plus the family of every failed job whose
    log was lost. The branch's named reds alone are not enough: when the loss swallowed the
    only red on the branch, H named none and scoped nothing, so a gate it could have moved was
    scored from other branches alone. A head whose lost log is attributed to no job scopes
    both families, because nothing proves it carried neither.
    """
    observed: list[tuple[frozenset[str], frozenset[str]]] = []
    for shas in _by_branch_sha(heads).values():
        draft = _is_draft(shas)  # a draft history is not scored, so it blinds nothing
        unread = _unread_shas(shas, window)
        named = frozenset(key for runs in shas.values() if _owned(runs, window) for h in runs.values() for key in h.red)
        for sha in unread:
            jobs: set[str] = set()
            present = shas[sha]
            for h in present.values():
                jobs |= h.unread_jobs
                if h.unread_logs and not h.unread_jobs:
                    jobs |= {QUALITY_JOB, *PYTEST_JOBS}
                if h.zero_jobs or (h.cancelled and not h.measured):
                    # A run whose evidence never existed — jobs read and empty, or cancelled
                    # before anything concluded (whose jobs are never fetched) — names no key.
                    # Every family its workflow carries is scoped; a cancellation a newer push
                    # superseded never reaches here (see _unread_shas).
                    jobs |= WORKFLOW_GATE_JOBS.get(h.workflow, frozenset())
                # A gating job this run skipped measured nothing: `ci gate` fails by design
                # when one is unexpectedly skipped, which leaves the run unmeasured with no
                # named red at all. Scope what each skipped job could have named, whatever
                # skipped it. A measured run with an explicit aggregate success scopes
                # nothing, and one whose aggregate is unknown or failed still withholds it.
                jobs |= _skipped_gate_scope(h)
            # A workflow with no run at all for this sha (not dispatched yet, or never
            # collected) is the same loss with no run to read: it could have failed any gate
            # it declares, so its families are scoped rather than leaving the head silent.
            for workflow in set(WORKFLOWS) - set(present):
                jobs |= WORKFLOW_GATE_JOBS.get(workflow, frozenset())
            observed.append((named, frozenset(jobs)))
        # A head that measured can still have withheld a family: its own aggregate is not a
        # success, so a gating job it skipped was never judged even though another job named a
        # red. It keeps the reds it named and the fix they carry (unlike an unread sha, which
        # leaves every count), so only the family the skip withheld is blind — never the
        # branch's other reds. Only a sha with a full verdict reaches this: a partial sha whose
        # missed runs were all cancellations is superseded, and its evidence contributes
        # nothing here either (the same rule `_unread_shas` applies to the pass above).
        for sha, present in shas.items():
            if draft or sha in unread or not full_verdict(present) or not _owned(present, window):
                continue
            withheld = frozenset().union(*(_skipped_gate_scope(h) for h in present.values()))
            if withheld:
                observed.append((frozenset(), withheld))

    def blind(key: str) -> int:
        return sum(1 for named_keys, jobs in observed if key in named_keys or any(_job_covers(j, key) for j in jobs))

    return blind


def _unread_reason_lines(meta: Mapping[str, object]) -> list[str]:
    """The count-by-reason sentence, or nothing when this run read every log it asked for.

    The counts cover every job log the run tried to read, look-behind included, so they sit
    next to the window-scoped `unreadable_logs` count rather than replacing it: a reader can
    see both how much evidence is missing from the scored window and why any is missing.
    """
    reasons = meta.get("unread_log_reasons") or {}
    if not isinstance(reasons, Mapping) or not reasons:
        return []
    samples = meta.get("unread_log_samples") or {}
    parts = []
    for reason, count in reasons.items():
        sample = samples.get(reason) if isinstance(samples, Mapping) else None
        if sample:
            detail = str(sample.get("detail", ""))[:160]
            parts.append(f"{reason} {count} (run {sample.get('run_id')} job {sample.get('job_id')}: {detail})")
        else:
            parts.append(f"{reason} {count}")
    return ["", "Unread log reasons: " + "; ".join(parts) + "."]


def _unread_head_reason_lines(meta: Mapping[str, object]) -> list[str]:
    """The count-by-reason sentence for incomplete heads that had no failed log read."""
    reasons = meta.get("unread_head_reasons") or {}
    if not isinstance(reasons, Mapping) or not reasons:
        return []
    samples = meta.get("unread_head_samples") or {}
    parts = []
    for reason, count in reasons.items():
        sample = samples.get(reason) if isinstance(samples, Mapping) else None
        if sample:
            parts.append(
                f"{reason} {count} ({sample.get('disposition')}; run {sample.get('run_id') or 'n/a'}: "
                f"{str(sample.get('detail', ''))[:160]})"
            )
        else:
            parts.append(f"{reason} {count}")
    return ["", "Unread head reasons: " + "; ".join(parts) + "."]


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
        f"{meta['unreadable_logs']}. Workflow reads unreadable: {meta['guard_reads_unread']}.",
        "",
        *_unread_reason_lines(meta),
        *_unread_head_reason_lines(meta),
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


def read_zero_jobs(run: Mapping, jobs: Mapping[int, list[dict]]) -> bool:
    """True only for a run whose jobs were read and came back empty. A run nothing read
    (a cancelled one, or a passing one on a branch with no red) is not evidence it had no
    job, so it must not claim its workflow's whole gate set."""
    return run["id"] in jobs and not jobs[run["id"]]


# A log body is PR-controlled bytes that end up as gate keys in generated markdown, so the
# strip has to cover every terminal control sequence, not only coloured text. Numeric CSI
# (`ESC [ 31m`) is the common case; these are the rest of the family:
#   * a control STRING introduced by `ESC ]` (OSC), `ESC P` (DCS), `ESC X` (SOS), `ESC ^`
#     (PM) or `ESC _` (APC) -- a window title, a hyperlink, a clipboard write -- ending in ST
#     (`ESC \`). BEL terminates ONLY OSC: a BEL inside a DCS/SOS/PM/APC payload is a control
#     executed inside the running string, so a shared `BEL|ST` terminator ended the string
#     early and its tail was parsed as log text, minting a gate key from PR bytes.
#   * CSI with ANY parameter/intermediate bytes, which covers the private forms (`?25l`,
#     `>0c`, `=1h`) and the colon sub-parameter form (`38:2::255:0:0m`).
#   * two-byte ESC sequences and charset designators (`ESC ( B`). The final byte is the
#     ECMA-48 range 0x30-0x7E, not just the `@`-`~` half: the digit and punctuation finals
#     (`ESC 7` DECSC, `ESC 8` DECRC, `ESC =` DECKPAM, `ESC >` DECKPNM) are real two-byte
#     sequences, and stopping the class at `@` left the final byte glued to the visible text,
#     so `[quality] \x1b7REGRESSION: ruff.style` parsed as `7REGRESSION` and the regression
#     vanished from `quality_keys`.
#   * the 8-bit C1 forms of the same introducers (`0x9b` CSI, `0x9d` OSC, `0x90`/`0x98`/
#     `0x9e`/`0x9f` DCS/SOS/PM/APC) and C1 ST (`0x9c`), all legal in a UTF-8 log.
#
# This is ONE scanner over the ECMA-48 state, not a pattern per byte class, because what the
# patterns kept getting wrong is not a character class but a state transition:
#   * CAN (`0x18`) and SUB (`0x1a`) CANCEL the sequence in progress and return the terminal to
#     ground, so what follows is VISIBLE text. A pattern that only knew terminators ran an
#     unterminated string to the newline and deleted the whole line, `[quality] REGRESSION`
#     marker included -- a PR-controlled byte hiding a red from the scorecard it exists for.
#   * a C0 control inside a CSI is EXECUTED and the sequence continues to its final byte:
#     `ESC [ 31 BEL m` is still SGR-red, so its `31m` must not survive as visible text.
#   * C0 inside a control-string payload is likewise executed, not a terminator; only BEL
#     (OSC) and ST end the string.
#   * an ESC inside a control-string payload ends the STRING, not just starts a two-byte
#     sequence: `ESC \` is ST, and any other byte aborts the string and is reprocessed as an
#     escape sequence (`ESC ] pay ESC [ 31 m` opens a CSI; `ESC ] pay ESC 7` is DECSC). While an
#     ESC was merely payload, the string ran on to the newline and deleted the whole line --
#     the same silent loss as an uncancelled CAN.
#   * an unterminated sequence never swallows the next line: a newline aborts it, and the
#     newline itself is kept because the parsers split on it. CR becomes `\n` (the cursor
#     returns to column 0, so what follows is a new line, not a continuation). Tab and every
#     byte that is not a control character are visible text.
#   * a cursor move that stays on ONE line is APPLIED, because it decides what the line shows:
#     BS and CUB (`ESC [ n D`) step back so the next character REPLACES the one it covered
#     (`REGX` BS `RESSION` is `REGRESSION`, and `::t` BS x6 `PASSED` is not a `FAILED` line),
#     CUF (`ESC [ n C`) and CHA (`ESC [ n G`) step forward over blanks, and EL (`ESC [ n K`)
#     blanks cells. A newline is the cursor reset to column 0, so no screen model is needed.
#     Dropping these bytes instead let a BS-covered character survive and hide a displayed
#     `[quality] REGRESSION` line from `quality_keys` (P2/BLOCKING at 1d4ca14b77).
#   * vertical and screen-level addressing stays OUT OF CONTRACT -- IND (`ESC D`), RI, NEL,
#     CUU/CUD, CNL/CPL, CUP/HVP, ED -- because one active line cannot represent it. A vertical
#     move is dropped, never applied as a horizontal step: IND read as a backstep would join
#     two terminal lines into one marker that no terminal shows.
#
# Byte values are compared by ordinal, so a byte that cannot belong to the sequence in
# progress aborts it and is re-scanned as text: the strip fails VISIBLE, never by eating a
# line it could not parse. Tab survives because `\S` classifies it as space; newline survives
# for the same reason the parsers need it.
_GROUND = 0  # between sequences: control bytes are dropped, everything else is text
_ESCAPE = 1  # after ESC: intermediates, an introducer, or one final byte
_CSI = 2  # after `ESC [` / C1 CSI: parameters and intermediates, then a final byte
_STRING = 3  # inside an OSC/DCS/SOS/PM/APC payload: consumed, never shown
_STRING_ESC = 4  # an ESC inside a payload: `ESC \` is ST, anything else aborts to _ESCAPE

_CANCEL = frozenset("\x18\x1a")  # CAN / SUB: cancel the sequence in progress
_ESC_STRING_INTRODUCERS = frozenset("PX^_]")  # ESC + one of these opens a control string
_C1_STRING_INTRODUCERS = frozenset("\x90\x98\x9d\x9e\x9f")  # the 8-bit DCS/SOS/OSC/PM/APC
# A control byte is where each state's work happens; everything between two of them is pure
# visible text in `_scan_ground`, so a multi-megabyte job log is copied in runs, not per char.
_SPECIAL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


@dataclass(frozen=True, slots=True)
class _CursorOp:
    """One line-local cursor operation: what a scanner emits and the driver applies.

    `count` is the sequence's own first numeric parameter and 0 means the log omitted one;
    ECMA-48's default differs per sequence (0 for EL, 1 for CUB/CUF/CHA), so `_Cursor` applies
    it rather than the scanner guessing it here.
    """

    kind: Literal["back", "forward", "column", "erase"]
    count: int = 0


_Emitted: TypeAlias = "str | tuple[str, _CursorOp]"


class _Cursor:
    """The text a terminal would leave on ONE line, and the cursor that writes it.

    Deliberately not an emulator: only the active line is held. That is what the parsers need,
    because each splits on `\n` and a line break IS the cursor reset to column 0 -- the same
    reset the pinned rows already give a ground CR (`FAILED tests/a.py\\rcontinuation`) and the
    byte CRLF (`a\\r\\nb`) is built from. What comes out is text the terminal would SHOW: a
    backstep replaces the character it covered instead of letting both survive, and `erase`
    blanks cells instead of deleting them, because the cell is still there on screen.
    """

    def __init__(self) -> None:
        self.out: list[str] = []
        self.line: dict[int, str] = {}
        self.col = 0

    def write(self, text: str) -> None:
        for ch in text:
            if ch == "\n":  # the line break IS the cursor reset to column 0
                self.out.append(self._render_line() + "\n")
                self.line, self.col = {}, 0
                continue
            self.line[self.col] = ch  # what the cursor stepped back over is REPLACED
            self.col += 1

    def apply(self, op: _CursorOp) -> None:
        if op.kind == "back":
            self.col = max(0, self.col - max(1, op.count))
        elif op.kind == "forward":
            self._seek(self.col + max(1, op.count))
        elif op.kind == "column":
            self._seek(max(1, op.count) - 1)
        else:  # erase in line
            self._erase(op.count)

    def _seek(self, target: int) -> None:
        """Keep the logical column without allocating cells for a forward gap.

        Later backsteps and overwrites use this actual position. Compressing it at seek time
        lets a backstep overwrite the preceding marker instead of blank cells. Only rendering
        collapses an unwritten gap to one separator, so hostile counts cannot size the buffer.
        """
        self.col = target

    def _erase(self, mode: int) -> None:
        if mode == 1:  # start of line to the cursor, inclusive
            for k in self.line:
                if k <= self.col:
                    self.line[k] = " "
        elif mode == 2:  # the whole line; the cursor does not move on EL
            self.line = dict.fromkeys(self.line, " ")
        else:  # cursor to end of line
            self.line = {k: ch for k, ch in self.line.items() if k < self.col}

    def _render_line(self) -> str:
        parts: list[str] = []
        previous = -1
        for column, ch in sorted(self.line.items()):
            if column > previous + 1:
                parts.append(" ")
            parts.append(ch)
            previous = column
        return "".join(parts)

    def finish(self) -> str:
        return "".join(self.out) + self._render_line()


def _csi_intro(text: str, final_at: int) -> str:
    """The parameter bytes of the CSI whose final byte is at `final_at`.

    Read back from the text instead of threaded through the `_CSI` state: the scan stops at the
    `[` that introduced the sequence (0x5B is outside the parameter range), so it costs only
    this sequence's own parameter bytes and the scanner signature every state shares stays put.
    """
    k = final_at
    while k > 0 and 0x20 <= ord(text[k - 1]) <= 0x3F:
        k -= 1
    return text[k:final_at]


def _csi_count(params: str) -> int:
    """A CSI's first numeric parameter, or 0 when the log omitted it (`ESC[K`, `ESC[D`)."""
    digits = "".join(c for c in params.split(";", maxsplit=1)[0] if c.isdigit()).lstrip("0")
    if len(digits) > 64:
        raise GhReadError("job log contains a CSI cursor count longer than 64 digits; cannot score this log")
    return int(digits) if digits else 0


def _after_cr(text: str, i: int) -> int:
    """The index after the CR at `i`, taking an immediately following LF with it.

    A CRLF pair is ONE line terminator, so the two bytes are consumed together: in ground text
    that is a single newline, and inside a sequence both halves are the payload's own bytes.
    Written once because every state needs the same pairing, and a CR left alone would hand its
    LF to the driver, which owns line breaks.
    """
    return i + 2 if text.startswith("\n", i + 1) else i + 1


def _scan_ground(text: str, i: int, osc: bool) -> tuple[int, bool, int, _Emitted]:
    """Copy visible text up to one control byte, and start the sequence it introduces."""
    match = _SPECIAL.search(text, i)
    if match is None:
        return _GROUND, osc, len(text), text[i:]
    start = match.start()
    visible = text[i:start]
    ch = text[start]
    nxt = start + 1
    if ch == "\r":  # cursor to column 0: in a log, the text after it starts a new line
        return _GROUND, False, _after_cr(text, start), visible + "\n"
    if ch == "\n" or ch in _CANCEL:  # the driver owns line breaks and cancellation
        return _GROUND, False, start, visible
    if ch == "\x1b":
        return _ESCAPE, osc, nxt, visible
    if ch == "\x9b":
        return _CSI, osc, nxt, visible
    if ch in _C1_STRING_INTRODUCERS:
        return _STRING, ch == "\x9d", nxt, visible
    if ch == "\x08":  # BS: step back one column, so the next write REPLACES what is there
        return _GROUND, osc, nxt, (visible, _CursorOp("back", 1))
    # Every other control byte (and the other C1 codes) is dropped; tab is real whitespace.
    return _GROUND, osc, nxt, visible + ("\t" if ch == "\t" else "")


def _scan_escape(text: str, i: int, osc: bool) -> tuple[int, bool, int, _Emitted]:
    """Consume one byte of a sequence introduced by ESC."""
    ch = text[i]
    code = ord(ch)
    if ch == "\x1b":  # a new ESC restarts the sequence
        return _ESCAPE, osc, i + 1, ""
    if ch == "[":
        return _CSI, osc, i + 1, ""
    if ch in _ESC_STRING_INTRODUCERS:
        return _STRING, ch == "]", i + 1, ""
    if 0x20 <= code <= 0x2F:  # intermediate: `ESC ( B` still has a final byte to come
        return _ESCAPE, osc, i + 1, ""
    if 0x30 <= code <= 0x7E:  # final byte: the sequence is complete
        return _GROUND, osc, i + 1, ""
    if ch == "\r":  # a C0 executed inside the sequence, which then continues
        return _ESCAPE, osc, _after_cr(text, i), ""
    if code < 0x20 or code == 0x7F:  # C0/DEL is executed inside the sequence
        return _ESCAPE, osc, i + 1, ""
    return _GROUND, osc, i, ""  # not part of this sequence: rescan it as text


# CSI finals this scanner APPLIES, because each one moves the cursor within the active line:
# CUB, CUF, CHA and EL. Everything else -- including the vertical movers -- is dropped.
_LINE_OPS: dict[str, Literal["back", "forward", "column", "erase"]] = {
    "D": "back",
    "C": "forward",
    "G": "column",
    "K": "erase",
}


def _scan_csi(text: str, i: int, osc: bool) -> tuple[int, bool, int, _Emitted]:
    """Consume one byte of a CSI; a C0 control inside it is executed, not a terminator."""
    ch = text[i]
    code = ord(ch)
    if ch == "\x1b":  # a new ESC abandons this CSI
        return _ESCAPE, osc, i + 1, ""
    if 0x20 <= code <= 0x3F:  # parameter and intermediate bytes
        return _CSI, osc, i + 1, ""
    if 0x40 <= code <= 0x7E:  # final byte
        # Only the finals that move the cursor WITHIN the active line are applied. CUU/CUD
        # (`A`/`B`), CNL/CPL (`E`/`F`), CUP/HVP (`H`/`f`) and ED (`J`) address other lines or
        # the whole screen, which the one-line model cannot represent; they stay dropped, as
        # they were, and are named out of contract in `strip_terminal_controls`.
        if ch in _LINE_OPS:
            count = _csi_count(_csi_intro(text, i))
            return _GROUND, osc, i + 1, ("", _CursorOp(_LINE_OPS[ch], count))
        return _GROUND, osc, i + 1, ""
    if ch == "\r":  # a C0 executed inside the CSI, which then continues
        return _CSI, osc, _after_cr(text, i), ""
    if code < 0x20 or code == 0x7F:  # executed inside the CSI, which then continues
        return _CSI, osc, i + 1, ""
    return _GROUND, osc, i, ""  # ST, a C1 introducer or corruption: abort, rescan as text


def _scan_string(text: str, i: int, osc: bool) -> tuple[int, bool, int, _Emitted]:
    """Consume one byte of a control-string payload; only ST -- and BEL, for OSC -- ends it."""
    ch = text[i]
    if ch == "\x1b":
        return _STRING_ESC, osc, i + 1, ""
    if ch == "\x9c" or (osc and ch == "\x07"):  # C1 ST, or BEL in the OSC it terminates
        return _GROUND, False, i + 1, ""
    if ch == "\r":  # payload, like every other C0 here: the string keeps running
        return _STRING, osc, _after_cr(text, i), ""
    return _STRING, osc, i + 1, ""


def _scan_string_esc(text: str, i: int, osc: bool) -> tuple[int, bool, int, _Emitted]:
    """Resolve the byte after an ESC inside a payload.

    `ESC \\` is ST and ends the string. ANY other byte -- a second ESC included -- aborts the
    string, and the byte is reprocessed in the ESCAPE state rather than consumed as payload:
    xterm's parser does `case OSC_STRING/SOS_PM_APC_STRING: if (c == ESC) state = ESCAPE`.
    Treating it as more payload ran the string on to the newline, so a real terminal shows a
    `[quality] REGRESSION` line that `quality_keys` never saw (Codex P2/BLOCKING at
    0de9f05689). Leaving `i` unconsumed is what makes `ESC ] pay ESC [ 31 m` open a CSI and
    `ESC ] pay ESC 7` the save-cursor sequence, without a special case per final byte.

    `osc` is returned untouched: it is read only inside `_STRING`, and every entry to that state
    re-derives it from the introducer that opened it, so it carries no meaning while in `_ESCAPE`.
    """
    ch = text[i]
    if ch == "\\":
        return _GROUND, False, i + 1, ""
    return _ESCAPE, osc, i, ""  # aborted: _scan_escape reprocesses this byte


_SCANNERS = {
    _GROUND: _scan_ground,
    _ESCAPE: _scan_escape,
    _CSI: _scan_csi,
    _STRING: _scan_string,
    _STRING_ESC: _scan_string_esc,
}


def strip_terminal_controls(text: str) -> str:
    """Remove every terminal control sequence and control byte from `text`.

    Applied to log bytes before they are parsed: a gate key and the markdown row it renders
    must not be able to carry a cursor move, an OSC 52 clipboard write or a bare CR. Text the
    terminal would SHOW survives, including text that follows a CAN/SUB cancellation.

    A cursor move that stays on the ACTIVE LINE is applied, because it changes what the line
    shows: BS and CUB step back so the next character REPLACES the one it covered (`REGX` BS
    `RESSION` shows `REGRESSION`; `::t` BS x6 `PASSED` shows no `::t`, so it mints no failure
    identity), CUF/CHA step forward, and EL blanks cells. A line break is the cursor reset to
    column 0, so this needs no screen model: a BS or CUB at column 0 stops there rather than
    stepping onto the line before.

    OUT OF CONTRACT, and deliberately left dropped: every move to another line or across the
    whole screen -- IND (`ESC D`), RI (`ESC C`/`ESC M`), NEL (`ESC E`), CUU/CUD
    (`ESC [ n A`/`ESC [ n B`), CNL/CPL (`ESC [ n E`/`ESC [ n F`), CUP/HVP (`ESC [ r ; c H`,
    `ESC [ r ; c f`) and ED (`ESC [ n J`). One active line cannot represent them, and applying
    a vertical move as if it were horizontal is itself a mint: reading IND as a one-column
    backstep turns two terminal lines into the single `[quality] REGRESSION` marker no
    terminal shows. Measured `ESC [ 1 ; 1 H` is byte-identical with and without the cursor, so
    nothing regresses; a log that needs real vertical addressing needs a tokenizer, not this.
    """
    # CR is NOT rewritten here: only `_scan_ground` knows a CR is ground text. A CR inside an
    # OSC/DCS/CSI is a C0 that executes and leaves the sequence running, so translating it to a
    # newline up front handed the driver an LF it read as the sequence's end and exposed a
    # payload no terminal shows -- one hidden `FAILED` line minted a gate key (P1/BLOCKING at
    # a6784645d6, sol-review line 881). `_scan_ground` emits the ground CR as `\n`, because a
    # cursor return means what follows starts a new line, and `_after_cr` takes a paired LF with
    # it so CRLF stays ONE line break. An LF is still a line boundary in a log: the driver ends
    # an unterminated sequence on it and keeps it, so one broken sequence cannot eat a log.
    cursor = _Cursor()
    state = _GROUND
    osc = False  # the current string is an OSC, where a bare BEL is a legal terminator
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\n":  # an unterminated sequence stops here; the line break is kept
            state, osc = _GROUND, False
            cursor.write(ch)
            i += 1
            continue
        if ch in _CANCEL:  # CAN/SUB cancel the sequence in progress; what follows is visible
            state, osc = _GROUND, False
            i += 1
            continue
        state, osc, i, emitted = _SCANNERS[state](text, i, osc)
        if isinstance(emitted, tuple):
            data, op = emitted
            cursor.write(data)
            cursor.apply(op)
        else:
            cursor.write(emitted)
    return cursor.finish()


# A job log is the one body that legitimately carries terminal escape sequences: the runner
# colors its step echo, so `gh api` refuses to print the log unless told to keep them, and a
# refusal is indistinguishable from an expired log to the caller -- every such log on main read
# as unreadable and voided the window it belonged to (GB-6870). The flag arrives with gh 2.97.0,
# the release that also neutralized escape sequences in `gh api` bodies; asking an older gh for
# it fails with `unknown flag`, which reads as an unread log for the wrong reason, so the local
# gh's own version is measured once and an older gh keeps main's unflagged read.
_JOB_LOG_PATH = re.compile(r"^repos/[^/]+/[^/]+/actions/jobs/\d+/logs$")
_ESCAPE_SEQUENCES_MIN_GH: tuple[int, int, int] = (2, 97, 0)


class GhReadError(Exception):
    """A read the scorecard must fail on rather than score through.

    Two shapes: a local gh that cannot be asked to keep a job log's escape sequences, and a body
    that is not valid UTF-8. `_job_log` reads any `RuntimeError` as an unreadable log (expired,
    or refused by the API), which is right for a 404 and wrong for both of these -- an unreadable
    job log voids the window it belongs to, so a cause the run could have refused up front must
    not be laundered into one. Deliberately not a `RuntimeError`, so that fallback cannot swallow
    it, and deliberately not `SystemExit`, so a caller can name it.
    """


def _fmt_version(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


@lru_cache(maxsize=8)
def _job_log_escape_flags(gh: str) -> tuple[str, ...]:
    """The flag that keeps a job log's escapes, or a named failure when this gh lacks it.

    Measured once per binary from its own `--version`, parsed by the same seam
    `scripts/gh_version_guard.py` uses. The shared floor there stays 2.64.0: `--slurp` still
    works on 2.64-2.96, and raising a floor is a decision, not a side effect of this read. What
    is not allowed is the fallback this used to take -- an older or unmeasurable gh reading the
    log WITHOUT the flag, which gh's own escape guard refuses, which `_job_log` then counts as an
    unreadable log, which voids the window (GB-6870, exactly the defect being fixed). So 2.97.0
    is required for THIS read and a gh below it, or one whose version cannot be measured, is a
    `GhReadError` naming the measured version and the fix.
    """
    proc = subprocess.run([gh, "--version"], capture_output=True, text=True, check=False)
    detail = proc.stderr.strip() or proc.stdout.strip() or "<no output>"
    if proc.returncode != 0:
        raise GhReadError(
            f"`{gh} --version` failed ({proc.returncode}): {detail[:200]}. A job log can only be "
            f"read with --allow-escape-sequences, added in gh {_fmt_version(_ESCAPE_SEQUENCES_MIN_GH)}; "
            f"refusing to fall back to the unflagged read that reads as an unreadable log."
        )
    try:
        version = parse_gh_version(proc.stdout)
    except GhVersionError as exc:
        raise GhReadError(
            f"cannot measure the gh at {gh}, so cannot tell whether it keeps a job log's escape "
            f"sequences: {exc}. Refusing to fall back to the unflagged read that reads as an "
            f"unreadable log."
        ) from exc
    if version < _ESCAPE_SEQUENCES_MIN_GH:
        raise GhReadError(
            f"gh {_fmt_version(version)} at {gh} cannot read a job log for the scorecard: "
            f"--allow-escape-sequences arrived in gh {_fmt_version(_ESCAPE_SEQUENCES_MIN_GH)}, and "
            f"without it gh refuses a colored log, which only counts as an unreadable log and voids "
            f"the window. Upgrade gh on this runner to "
            f"{_fmt_version(_ESCAPE_SEQUENCES_MIN_GH)} or newer (the shared --slurp floor of "
            f"{_fmt_version(MIN_GH_VERSION)} is unchanged)."
        )
    return ("--allow-escape-sequences",)


def _gh_api(path: str) -> str:
    gh = shutil.which("gh")
    if gh is None:
        raise SystemExit("[ERROR] gh is not on PATH; the scorecard reads GitHub through `gh api`")
    # The flag belongs on the job-log fetch ONLY: on any other path it disables gh's own
    # protection against control bytes for a body that has no legitimate use for them.
    flags = _job_log_escape_flags(gh) if _JOB_LOG_PATH.match(path) else ()
    proc = subprocess.run([gh, "api", *flags, path], capture_output=True, check=False)
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()[:200]
        raise RuntimeError(f"gh api {path} failed: {detail}")
    try:
        # Strict: `errors="replace"` rewrote undecodable bytes into U+FFFD before
        # `failed_identities` parsed them, which can mint or mangle a gate key out of bytes no run
        # ever printed. A log that will not decode is a collection failure, not a log.
        body = proc.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GhReadError(
            f"gh api {path} returned bytes that are not valid UTF-8 ({exc.reason} at byte "
            f"{exc.start}); refusing to rewrite them into a gate key this run never measured"
        ) from exc
    if _JOB_LOG_PATH.match(path):
        # With `--allow-escape-sequences` the log's control bytes reach us intact, and a parsed
        # identity becomes a gate key rendered into generated markdown: neutralise them here, so
        # no parser downstream can see a cursor move, an OSC 52 clipboard write or a bare CR.
        body = strip_terminal_controls(body)
    return body


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
    # Runs whose workflow-definition read failed where a skip's cause had to be proved: their
    # skip evidence is UNKNOWN, so they are counted here and in `incomplete_heads`, never
    # quietly treated as a run whose skips were all independent.
    guard_reads_unread: int = 0
    # Why each unread log was unread, counted by class, with the first entry of each class for
    # its run id, job id and gh message. Covers every log this run tried to read (the
    # look-behind included), which is why it is reported beside, not instead of, the
    # window-scoped `unreadable_logs` count.
    unread_reasons: dict[str, int] = field(default_factory=dict)
    unread_samples: dict[str, dict] = field(default_factory=dict)


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


LOG_RETRY_BACKOFF_S = (0.5, 1.5)


# A dropped or stalled connection, as gh and the HTTP stack word it. One list, so the
# retry decision (`_log_failure_is_transient`) and the reported class (`unread_reason`)
# cannot drift. Substrings: "timeout" also covers "i/o timeout" and "tls handshake timeout".
TRANSPORT_ERRORS: tuple[str, ...] = (
    "timed out",
    "timeout",
    "connection reset",
    "connection refused",
    "error connecting",
    "stream error",
    "received from peer",
    "unexpected eof",
    "temporary failure",
)


def _log_failure_is_transient(err: RuntimeError) -> bool:
    """Whether a failed log fetch can heal on its own and is worth another attempt.

    Only a 5xx from the artifacts host or a dropped connection is retried. An expired log
    answers 404/410 and a permission answer is 403: retrying those spends API calls on a
    result that cannot change, and on a token this routine shares with every other thread
    that waste is the failure mode that matters. Anything unrecognised stays permanent, so
    an unknown error is never multiplied by three.
    """
    text = str(err).lower()
    if any(seen in text for seen in ("http 404", "http 410", "http 403", "http 422", "not found", "gone")):
        return False
    if any(f"http {code}" in text for code in (500, 502, 503, 504)):
        return True
    return any(seen in text for seen in TRANSPORT_ERRORS)


UNREAD_REASON_ORDER: tuple[str, ...] = (
    "expired",
    "forbidden",
    "rate_limited",
    "server_error",
    "transport",
    "empty",
    "other",
)


def unread_reason(err: RuntimeError | None) -> str:
    """Why one job log could not be read, classified so the report names the cause.

    Only the counts were reported before, so a window full of UNKNOWN verdicts could not
    say whether its evidence had expired, been refused, or never arrived. The classes are
    the ones with a different next action: `expired` is unrecoverable (the artifact is
    gone), `forbidden` is a token scope, `rate_limited` and `server_error` are the shared
    token's own load, `transport` is the network, and `empty` is a 200 with no body.
    An unrecognised message stays `other`, with its first line kept in the entry, so a new
    cause is visible instead of being folded into a known one.
    """
    if err is None:
        return "empty"
    text = str(err).lower()
    if any(seen in text for seen in ("http 404", "http 410", "not found", "gone")):
        return "expired"
    if any(seen in text for seen in ("http 403", "forbidden")):
        return "forbidden"
    if any(seen in text for seen in ("http 429", "rate limit", "secondary rate")):
        return "rate_limited"
    if any(f"http {code}" in text for code in (500, 502, 503, 504)) or "bad gateway" in text:
        return "server_error"
    if any(seen in text for seen in TRANSPORT_ERRORS):
        return "transport"
    return "other"


@dataclass
class UnreadLogs:
    """One entry per job log this run could not read, with its classified reason.

    `_job_log` runs inside a `ThreadPoolExecutor`, so this is an append-only list and the
    counts are derived once the pool has joined: no counter is mutated from several
    threads, and no entry is lost to a read-modify-write.
    """

    entries: list[dict] = field(default_factory=list)

    def record(self, job: Mapping, reason: str, detail: str) -> None:
        self.entries.append(
            {
                "run_id": int(job.get("run_id") or 0),
                "job_id": int(job.get("id") or 0),
                "job": str(job.get("name") or ""),
                "reason": reason,
                "detail": detail.splitlines()[0][:200],
            }
        )

    def counts(self) -> dict[str, int]:
        counted = Counter(entry["reason"] for entry in self.entries)
        return {reason: counted[reason] for reason in UNREAD_REASON_ORDER if counted.get(reason)}


def _first_by_reason(entries: Sequence[Mapping]) -> dict[str, dict]:
    """The first entry of each reason class, in `UNREAD_REASON_ORDER`, for the report line."""
    samples: dict[str, dict] = {}
    for entry in entries:
        samples.setdefault(str(entry["reason"]), dict(entry))
    return {reason: samples[reason] for reason in UNREAD_REASON_ORDER if reason in samples}


def _job_log(api: Api, job: Mapping, unread: UnreadLogs | None = None) -> tuple[int, str | None]:
    """One job's log, or None when it cannot be read (expired, or the API refused).

    A transient failure is retried with backoff, because those logs do come back and the
    window they belong to is void while they are missing. A permanent one returns on the
    first attempt, so the retry cannot lengthen a run whose logs are simply gone.
    `unread` collects the run id, job id and classified reason for every None returned, so
    the report can say why its evidence is missing rather than only how much is.

    `GhReadError` is not one of those: it says this run's own read is impossible (a gh that
    cannot keep a log's escapes, or bytes that will not decode), so it propagates and the run
    fails instead of scoring a window it could not read. It is not a `RuntimeError`, so the
    retry below cannot swallow it.
    """
    last: RuntimeError | None = None
    for wait in (0.0, *LOG_RETRY_BACKOFF_S):
        if wait:
            time.sleep(wait)
        try:
            body = api(f"repos/{REPO}/actions/jobs/{job['id']}/logs") or None
            if body is None and unread is not None:
                unread.record(job, "empty", "gh api returned an empty body")
            return job["id"], body
        except RuntimeError as err:
            last = err
            if not _log_failure_is_transient(err):
                if unread is not None:
                    unread.record(job, unread_reason(err), str(err))
                return job["id"], None
    if unread is not None:
        unread.record(job, unread_reason(last), str(last) if last else "<no error recorded>")
    return job["id"], None


def _api_json(api: Api, path: str) -> dict:
    """One JSON object read, retried only while the failure can heal on its own.

    `jobs_of` runs inside `ThreadPoolExecutor.map`, so a single raise there escapes the pool
    before `unmeasured_window` is consulted: the run ends on a traceback with no exit code
    and no cause counts, which reads as "produced nothing". A 502 from the API is the
    ordinary transient case, so the listing carries the same bounded backoff as `_job_log`;
    an answer that cannot change (404/403/422) still returns on the first attempt, so a gone
    run costs no extra calls on the token every other thread shares.
    """
    last: RuntimeError | None = None
    for wait in (0.0, *LOG_RETRY_BACKOFF_S):
        if wait:
            time.sleep(wait)
        try:
            return json.loads(api(path))
        except json.JSONDecodeError as err:
            raise RuntimeError(f"gh api {path} returned invalid JSON: {err}") from err
        except RuntimeError as err:
            last = err
            if not _log_failure_is_transient(err):
                raise
    if last is None:  # pragma: no cover - the loop always sets it before falling through
        raise RuntimeError(f"gh api {path} failed with no error recorded")
    raise last


def _agreed_guards(*maps: Mapping[str, Mapping[str, bool]]) -> dict[str, dict[str, bool]]:
    """The step declarations every revision in `maps` agrees on.

    A pull_request run executes a merge of its head into its base, and GitHub publishes
    neither that merge commit on the run nor a ref bound to it (`refs/pull/<n>/merge` is the
    latest simulated merge, `pulls/<n>.merge_commit_sha` the eventual integration), so a read
    of either can see a different `if:` than the run used. A clean three-way merge can only
    keep a step unguarded when both parents declare it with no condition of its own — had one
    parent added an `if:`, its own file would say so — and a conflicted file stops the merge
    from existing, so the run would not have started. Agreement across the parents is
    therefore the proof. A step they disagree on, or a job one of them does not declare,
    proves nothing and is left out, which keeps its skip independent withheld evidence.
    """
    out: dict[str, dict[str, bool]] = {}
    for base, steps in maps[0].items():
        others = [m.get(base) for m in maps[1:]]
        if any(other is None for other in others):
            continue
        agreed = {
            step: False
            for step, guarded in steps.items()
            if guarded is False and all(other.get(step) is False for other in others if other is not None)
        }
        if agreed:
            out[base] = agreed
    return out


def guard_proof(api: Api, workflow: str, run: Mapping) -> dict[str, dict[str, bool]] | None:
    """The step-guard declarations that governed `run`, or None when they cannot be proven.

    A `pull_request` run executes the merge of its head into its base, so its steps ran under
    the merge's `if:` declarations, not the branch head's. GitHub does not publish that merge
    commit for the run, nor — once the PR closes — the base revision this run carried, and both
    `refs/pull/<n>/merge` and `pulls/<n>.merge_commit_sha` track the base as it moved. So no
    stand-in revision is read: the head and the base the run itself carried are read and only
    what they agree on is proven (`_agreed_guards`). A closed run's own `pull_requests` field is
    empty, and the recovered PR would only carry today's base, so nothing is proven for it —
    an empty map, which keeps its skips independent withheld evidence without voiding the facts
    the run did measure. A payload naming several PRs, a missing base, or a revision that
    cannot be read is None: the caller reports an unproven read rather than guessing one.
    """
    if run["event"] != "pull_request":
        return workflow_step_guards(api, workflow, str(run["head_sha"]))
    pulls = run.get("pull_requests") or []
    if not pulls:
        return {}
    if len(pulls) != 1:
        return None
    base = (pulls[0].get("base") or {}).get("sha")
    if not base:
        return None
    maps = [workflow_step_guards(api, workflow, str(sha)) for sha in (run["head_sha"], base)]
    if any(one is None for one in maps):
        return None
    return _agreed_guards(*[one for one in maps if one is not None])


def _selection_branches(
    wanted: Sequence[tuple[str, dict]], verdicts: Mapping[int, tuple[frozenset[str], bool, int, frozenset[str]]]
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
    verdicts: Mapping[int, tuple[frozenset[str], bool, int, frozenset[str]]],
    logs: Mapping[int, str | None],
    workers: int,
    unread: UnreadLogs | None = None,
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
        return dict(pool.map(lambda j: _job_log(api, j, unread), shards))


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
        body = _api_json(api, f"repos/{REPO}/actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs?per_page=100")
        return run["id"], body["jobs"]

    with ThreadPoolExecutor(workers) as pool:
        jobs = dict(pool.map(jobs_of, failed))

    # A skip after an in-job failure is only the failure's shadow if the workflow that ran
    # proves the step has no condition of its own. GitHub does not publish the merge commit a
    # pull_request run executed, so the proof is what its head and base revisions agree on
    # (`guard_proof`); a push run reads its own head. Read once per such run, and only where a
    # shadow is even possible. A refused or unparseable file leaves the entry at None, and that
    # run is then reported as unproven skip evidence (`guard_unread`), never a silent skip.
    shadow_runs = {
        r["id"]: (w, r) for w, r in failed if any(_skips_after_a_failure(j) for j in jobs.get(r["id"], []))
    }

    def guards_of(item: tuple[str, dict]) -> tuple[int, dict[str, dict[str, bool]] | None]:
        workflow, run = item
        return run["id"], guard_proof(api, workflow, run)

    with ThreadPoolExecutor(workers) as pool:
        step_guards = dict(pool.map(guards_of, shadow_runs.values()))

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

    unread_logs = UnreadLogs()
    with ThreadPoolExecutor(workers) as pool:
        logs = dict(pool.map(lambda j: _job_log(api, j, unread_logs), log_jobs))

    verdicts = {run["id"]: judge_jobs(jobs.get(run["id"], []), logs) for _, run in wanted}
    selection = _selection_logs(api, wanted, jobs, verdicts, logs, workers, unread_logs)
    logs = {**logs, **selection}
    consulted = _selection_branches(wanted, verdicts)

    heads: list[Head] = []
    main_red: Counter[str] = Counter()
    unreadable = 0
    main_unread = 0
    guard_unreads = 0
    log_ids = {j["id"] for j in log_jobs}
    reported: list[dt.datetime] = []
    for workflow, run in wanted:
        red, measured, missing, unread_jobs = verdicts[run["id"]]
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
        # The workflow that ran could not be read, so a skip behind a failure in the same job
        # cannot be told from one whose own `if:` was false. The run's skip evidence is
        # unproven: count it and leave the run unmeasured rather than reading it as independent.
        guard_unread = run["id"] in step_guards and step_guards[run["id"]] is None
        if guard_unread:
            measured = False
            guard_unreads += 1
        # A shard's unread or missing selection report would read as "ran no modules", so it
        # is an unread log like any other: it fails the window rather than dropping a clear.
        failed_logs = logs if _branch_key(run) in consulted else {}
        sel_missing = unread_selections(jobs.get(run["id"], []), selection, failed_logs)
        unreadable += sel_missing
        missing += sel_missing
        # A selection report is read from the pytest job's own log, so losing it loses a
        # pytest shard's evidence exactly as an unread failure log does: the run gave no
        # verdict, so the head leaves every count instead of scoring the reds it did name.
        lost_jobs = set(unread_jobs)
        if sel_missing:
            measured = False
            lost_jobs |= PYTEST_JOBS
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
                status=run.get("status"),
                conclusion=run.get("conclusion"),
                unread_jobs=frozenset(lost_jobs),
                zero_jobs=read_zero_jobs(run, jobs),
                aggregate=run_aggregate_verdict(jobs.get(run["id"], [])),
                guard_unread=guard_unread,
                skipped_behind_failure=skips_behind_a_failure(jobs.get(run["id"], []), step_guards.get(run["id"])),
            )
        )
    uncovered = sum(
        _in_main_window(r, main_window) and not any(t > _iso(r["created_at"]) for t in reported) for r in cancelled_main
    )
    unreadable += uncovered
    main_unread += uncovered
    return Collected(
        heads,
        main_red,
        len(wanted),
        unreadable,
        len(log_jobs),
        main_unread,
        guard_unreads,
        unread_logs.counts(),
        _first_by_reason(unread_logs.entries),
    )


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


def owned_guard_unreads(heads: Sequence[Head], window: tuple[dt.datetime, dt.datetime] | None) -> int:
    """Runs on the PR heads whose verdict completed in `window` whose workflow-definition read
    failed, so their skip evidence is UNKNOWN rather than a measured independent skip."""
    return sum(
        h.guard_unread
        for shas in _by_branch_sha(heads).values()
        for runs in shas.values()
        if _owned(runs, window)
        for h in runs.values()
    )


def unmeasured_window(data: Collected, window: tuple[dt.datetime, dt.datetime] | None = None) -> str | None:
    """Why a window cannot be scored at all, or None. A window with no sha holding a measured
    verdict from every workflow (startup failures, timed-out gating jobs, every e2e run
    cancelled) scores nothing, so it is an outage, never an empty scorecard. So is a window
    with an unreadable main log: main's red count is a window-wide denominator that no PR head
    can stand in for.

    An unreadable *PR* log is no longer a window-wide outage: it is scoped per gate by
    `unread_scopes`, and the gates it cannot touch are scored as usual."""
    if not owned_shas(data.heads, window):
        return "no PR head in the window has a measured verdict from every workflow"
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
    red_heads, ratchet_only_heads = head_summary(data.heads, window)
    unread_head_counts, unread_head_samples = unread_head_reasons(data.heads, window)
    reason = unmeasured_window(data, window)
    meta = {
        "date": until.date().isoformat(),
        "since": since.isoformat(),
        "until": until.isoformat(),
        "fixes_read_until": now.isoformat(),
        "heads": len(owned_shas(data.heads, window)),
        "red_heads": red_heads,
        "ratchet_only_heads": ratchet_only_heads,
        "unreadable_logs": owned_unread_logs(data.heads, window)[0] + data.main_unread_logs,
        "unread_log_reasons": data.unread_reasons,
        "unread_log_samples": data.unread_samples,
        "unread_head_reasons": unread_head_counts,
        "unread_head_samples": unread_head_samples,
        "guard_reads_unread": owned_guard_unreads(data.heads, window),
        "incomplete_heads": incomplete_heads(data.heads, window),
    }
    if reason:
        meta["unknown_reason"] = reason
        markdown = render_markdown({}, meta)
        if args.json:
            args.json.write_text(
                json.dumps({"meta": meta, "gates": []}, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
                newline="\n",
            )
        if args.markdown:
            args.markdown.write_text(markdown, encoding="utf-8", newline="\n")
        sys.stdout.write(markdown)
        print(f"[ratchet-scorecard] UNKNOWN: {reason} between {since} and {until}", file=sys.stderr)
        return 3
    scores = score(data.heads, data.main_red, own_files(api), now, window=window, horizon=LOOKAHEAD)
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
    # A read this run cannot make is a named failure, not a traceback and not a window scored
    # from logs it could not read: exit 3 already means "the window is UNKNOWN", so this is 4.
    try:
        raise SystemExit(main())
    except GhReadError as exc:
        sys.stderr.write(f"[ratchet-scorecard] ERROR: {exc}\n")
        raise SystemExit(4) from None
