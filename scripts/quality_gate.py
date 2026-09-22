"""
quality_gate.py -- programmatic code-quality evaluators with a regression ratchet.

Answers one question on every run: "is this tree messier or more tangled than
it was last time?" It does NOT try to answer "is this tree clean?", because it
is not, and a gate that fails on day one gets switched off by day two. Every
metric is compared against ops/quality/baseline.json and only a WORSE number
fails. Better numbers print as a ratchet opportunity and are written down by
`--update-baseline`, so the allowance only ever shrinks.

Requirements (status key: -> out-of-scope, ? todo, OK done, RUN done+ran,
REG done+ran+regression-tested):

REG  Q-01 Measure Python lint debt bucketed by what each rule family catches
          (complexity, coupling, safety, correctness, style), via pinned ruff.
          [if a new `except Exception:` lands then ruff.safety rises and the
           run exits 1]
          [if a rule family reports fewer violations then the run prints
           RATCHET and still exits 0]
          [if ruff is unpinned or missing then the run aborts, never scores 0]
REG  Q-02 Measure Python structural complexity: worst cyclomatic block, count
          of blocks over the mccabe threshold, count of low-maintainability
          files, via pinned radon.
          [if a 60-branch function is added then complexity.blocks_over_limit
           rises and the run exits 1]
          [if radon emits a parse error then the run aborts rather than
           reporting max complexity 0]
          [if a hot function is split into three then the run prints RATCHET]
REG  Q-03 Enforce architecture as a hard gate (no ratchet) via import-linter
          contracts in .importlinter, plus count package-level import cycles.
          [if apps.shared imports apps.webui then contracts_broken is 1 and the
           run exits 1 regardless of baseline]
          [if a new domain package imports apps.webui then the run exits 1]
          [if a documented debt import is deleted then contracts still pass]
REG  Q-04 Measure frontend coupling and dead code: import cycles across BOTH
          .ts and .svelte, plus knip unused files/exports/dependencies.
          [if two components import each other then frontend.import_cycles
           rises above baseline and the run exits 1]
          [if a component is orphaned then frontend.unused_files rises]
          [if a .svelte import is added then it appears in the graph, unlike
           madge which parses zero imports out of .svelte and reports a false
           clean]
REG  Q-05 Measure file bloat and copy-paste: longest file per language, count
          over the review threshold, and jscpd duplicated-line percentage.
          [if a 4000-line module grows then file_size.max_frontend rises]
          [if a block is pasted into a second file then duplication.percent
           rises and the run exits 1]
          [if a long file is split then the run prints RATCHET]
OK   Q-06 Report (never gate) change hotspots: git churn multiplied by file
          size, so refactoring effort lands where the mess is actually edited.
RUN  Q-07 Emit a machine-readable JSON result and a human markdown report so
          both CI and a person get the same numbers from one run.
REG  Q-09 Measure Python type debt with a pinned mypy, split by top-level
          path so test-fixture debt cannot mask a production regression, with
          the count of files mypy actually checked as an ungated control.
          [if an untyped return value is assigned in apps/ then
           mypy.errors_apps rises and the run exits 1]
          [if the scanned scope is narrowed then mypy.files_checked drops and
           the falling error counts read as a narrowed gate, not a cleanup]
          [if mypy exits on a config error then the run aborts rather than
           reporting zero type errors]
          [if MYPYPATH or PYTHONPATH is set in the calling shell then the
           isolated run still measures the pinned tree, not the ambient
           search path]
REG  Q-08 Enforce four shell constructs as a hard gate (no ratchet) via
          scripts/shell_construct_lint.py, over shell sources and justfile
          recipes. Each one returns a plausible value with no error, so a
          growing allowance for them is not a rule.
          [if `$?` is read after a pipeline without pipefail then
           shell.pipeline_status is 1 and the run exits 1 regardless of
           baseline]
          [if `gh api` is passed --arg then shell.gh_api_arg is 1 and exits 1]
          [if discovery collapses to fewer files than the linter's floor then
           the run aborts rather than scoring 0 violations]
REG  Q-10 Measure frontend type-erasure: `as unknown as` double-casts across
          hand-written .ts/.svelte sources. A single `as` is a claim the
          compiler still checks for overlap; the double form asserts through
          `unknown` and therefore checks nothing at all, which is how a
          response type can drift away from its schema in silence.
          [if a new `as unknown as` lands then frontend.unknown_casts rises
           and the run exits 1]
          [if a double-cast is replaced by a generated-schema alias then the
           run prints RATCHET and still exits 0]
          [if the frontend scan finds no sources then the run aborts rather
           than scoring 0 casts on an empty tree]
REG  Q-11 Tell an inherited trunk regression from one this change introduced.
          A merge result can sit over an allowance that NEITHER parent exceeded
          (issue #1155: two sides each add lines to the same file, each stays
          under the limit, the union crosses it), and once that lands on main
          every later PR reads the same over-allowance metric as its own fault.
          On a REGRESSION the gate re-measures that metric on the merge-base
          main. If main is already AT OR ABOVE this run's value the line prints
          INHERITED and does not fail the run - it is trunk's regression, not
          this change's. If this run is ABOVE main's value the change made an
          already-over metric worse and it stays a hard REGRESSION.
          [if the merge-base main is already at this run's value then the line
           prints INHERITED and the run exits 0]
          [if this change adds to an already-over metric (base below the run)
           then the run exits 1]
          [if the merge base cannot be measured then base_compare reports
           UNKNOWN (exit 2), never REGRESSION, and the output says why]
REG  Q-12 Judge a run against its allowance PLUS a small declared slack, so a
          normal PR can land while debt still trends down (issue #1219: all
          seven count metrics sat at zero headroom at once because each
          landing PR lowered the allowance to whatever it achieved, and the
          wall therefore moved with every burn-down). Slack is a per-metric
          constant in baseline.json's `slack` block, worth roughly one
          ordinary PR, justified in ops/quality/README.md. Allowances still
          only shrink; the band is headroom at gate time, never a recorded
          allowance, and a metric with no slack entry gets zero.
          [if a metric lands exactly at allowance + slack then the run prints
           WITHIN SLACK with both raw numbers and exits 0]
          [if it lands one past allowance + slack then the run exits 1]
          [if baseline.json declares no slack for a metric then that metric is
           gated at its bare allowance, exactly as before]
          [if --update-baseline rewrites the file then the `slack` block
           survives, so a ratchet-down cannot silently re-tighten to zero]
          [if --update-baseline runs on a tree whose metric sits inside its
           slack band then the recorded allowance is KEPT, not raised to the
           measurement, so repeated updates cannot walk the ceiling upward]
REG  Q-13 Enforce sync-schema drift as a hard gate (no ratchet) via
          scripts/sync_drift_lint.py, over a state DB provisioned by all
          seven of its schema authorities. Every defect it names is an
          OMISSION -- a forgotten SCHEMA_VERSION bump, a table never added to
          SYNC_TABLES, an authority no inventory declares -- and an omission
          raises nothing, so an allowance for one is an allowance for silence.
          [if a table carries the sync columns and no registry lists it then
           sync_drift.unregistered_synced_table is 1 and the run exits 1
           regardless of baseline]
          [if a MIGRATIONS step is appended without a SCHEMA_VERSION bump then
           sync_drift.version_ladder_mismatch is 1 and exits 1]
          [if the provisioned DB lacks a table ALL_KNOWN_TABLES declares then
           the run aborts rather than scoring 0 violations]
          [if a drift check stops being emitted at all then the evaluator
           raises rather than reporting the remaining zeros]

Usage:
    python -m scripts.quality_gate                       # gate against baseline
    python -m scripts.quality_gate --only arch,frontend  # subset
    python -m scripts.quality_gate --update-baseline     # ratchet down
    python -m scripts.quality_gate --report ops/quality/report.md
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from scripts import quality_latency, shell_construct_lint
except ModuleNotFoundError as exc:
    if exc.name == "scripts":
        raise SystemExit("uv run --no-sync python -m scripts.quality_gate") from None
    raise

# ----- config --------------------------------------------------------------

REPO = Path(__file__).resolve().parent.parent
FRONTEND = REPO / "apps" / "webui" / "frontend"
TOOL_REQS = REPO / "ops" / "quality" / "requirements.txt"
MYPY_REQS = REPO / "ops" / "quality" / "mypy-requirements.txt"
BASELINE = REPO / "ops" / "quality" / "baseline.json"
# Ref the gate re-measures a regression against, to tell an inherited trunk
# failure from one this change introduced (Q-11). Named here so a test or a
# fork can point it at a different upstream without editing the call sites.
MAIN_BASE_REF: str = "origin/main"

# `--isolated` pins uv's *package* set but forwards the caller's environment
# unchanged, and mypy reads these to widen its own import search path outside
# any package manager. A `.venv`-free host still measures a different tree if
# either is set: pointing MYPYPATH at one throwaway directory holding a single
# unrelated module moved mypy.errors_scripts 107 -> 117 on this exact tree,
# reproduced via _eval_mypy() itself, no source change.
IMPORT_PATH_ENV_VARS: tuple[str, ...] = ("MYPYPATH", "PYTHONPATH")


class CFG:
    """Thresholds and pins. Change these deliberately, then reprint baseline."""

    PY_PATHS: tuple[str, ...] = ("apps", "tests", "scripts")
    PY_LINT_PATHS: tuple[str, ...] = ("apps", "tests", "scripts", "conftest.py")
    # Must match [tool.ruff.lint.mccabe] max-complexity in pyproject.toml.
    MAX_COMPLEXITY: int = 12
    # "This file is too long to hold in your head" line, per language.
    PY_FILE_LIMIT: int = 600
    FE_FILE_LIMIT: int = 600
    # jscpd: a clone has to be this big before it is worth naming.
    DUP_MIN_LINES: int = 30
    DUP_MIN_TOKENS: int = 100
    # mypy runs from its own pinned env and takes its scope from
    # [tool.mypy] files in pyproject.toml, so the gate hands it flags only.
    # Restating the paths here would let the command line override the config
    # and let the two disagree; tests/quality/test_mypy_scope.py enforces that.
    MYPY_FLAGS: tuple[str, ...] = ("--output", "json", "--no-color-output")
    # Floor under the mypy control metric. mypy.files_checked is report-only,
    # so it SHOWS a narrowed scope but cannot fail on one. This aborts instead,
    # for the same reason _eval_shell aborts on an empty scan: a gate that
    # measured almost nothing must not report a clean tree. 1178 modules are
    # checked as of Tue 1 Sep 2026; the floor is loose enough to survive real
    # deletions and tight enough to catch a scope that collapsed.
    MYPY_MIN_FILES: int = 900
    # Hotspot report window.
    CHURN_DAYS: int = 90
    HOTSPOT_COUNT: int = 12
    # Frontend tool pins. Python pins live in ops/quality/requirements.txt.
    KNIP: str = "knip@6.32.2"
    JSCPD: str = "jscpd@5.0.15"
    # Lockfiles are repetitive by construction. jscpd scoring them is not a
    # duplication finding anyone can act on, and it made committing the
    # launcher lockfile a CI failure. Class-wide, not the one file that
    # happened to hurt: the next committed lockfile will be someone else's PR.
    LOCKFILE_GLOBS: tuple[str, ...] = (
        "**/pnpm-lock.yaml",
        "**/package-lock.json",
        "**/yarn.lock",
        "**/Cargo.lock",
        "**/uv.lock",
    )
    # Generated contract artifacts, ignored for duplication on the same
    # argument as the lockfiles above: nobody writes them, `just pre-push` and
    # CI do, and every endpoint in them repeats the same 422 response block by
    # construction. Sat 19 Sep 2026 made the cost concrete -- main's committed
    # openapi.json had been truncated from 286 paths to 134, breaking contract
    # drift on every PR, and restoring the 150 missing endpoints moved
    # duplication.percent from 0.31 to 0.39. The gate would have blocked the
    # repair of its own trunk. Scored without these two: 0.21.
    GENERATED_CONTRACT_GLOBS: tuple[str, ...] = (
        "**/webui/openapi.json",
        "**/src/lib/api-types.ts",
    )
    # Paths excluded from size/complexity scoring: vendored, not ours.
    #
    # Third-party source only. "rb_vendor" in a filename is not a licence:
    # apps/webui/server/rb_vendor.py sat here until T3b and it is first-party
    # business logic (CAS hot-cue writes, reversible undo, ANLZ caching,
    # waveform decode). Exempting it is the most plausible reason it reached
    # 2,267 lines without the 600-line gate ever firing. It is now scored like
    # everything else, and its debt is carried in ops/quality/baseline.json as
    # a number that only shrinks, which is what makes the T3b split a
    # burn-down instead of a promise.
    VENDORED: tuple[str, ...] = (
        "apps/sync/usb/pioneer/_vendor",
    )
    # Build OUTPUT that happens to land under a scored path. Neither
    # third-party source nor ours: derived bytes, gitignored, rebuilt from
    # scratch by `just dmg`. The desktop payload stages a relocatable CPython
    # plus the whole installed dependency closure into
    # apps/desktop/src-tauri/payload, and tauri-build mirrors it into target/
    # for dev runs -- ~100MB of numpy, uvicorn and sqlalchemy that would
    # otherwise be scored as this repo's own complexity debt. radon found it
    # first, by failing to parse numpy's .pxd files: the gate crashed rather
    # than merely lying, which is the better of the two outcomes but still
    # not the right one.
    DERIVED: tuple[str, ...] = (
        "apps/desktop/src-tauri/payload/",
        "apps/desktop/src-tauri/target/",
    )


# Metrics that must be exactly zero, with no baseline allowance. An
# architecture rule with a growing allowance is not a rule, and neither is a
# ban on a construct that silently returns the wrong answer.
HARD_ZERO: frozenset[str] = frozenset({
    "arch.contracts_broken",
    "shell.pipeline_status",
    "shell.gh_api_arg",
    "shell.zsh_modifier_path",
    "frontend.ts_escape_hatches",
    # Sync-schema drift. Same reasoning one step further: every one of these
    # is an OMISSION, and an omission raises nothing at runtime, so "we are
    # allowed two unregistered synced tables" is an allowance for silence.
    #
    # This list is ALSO the independent declaration of which drift rules must
    # exist: _eval_sync_drift refuses to return unless every key below was
    # actually emitted. Without that, deleting a check from the linter's own
    # CHECKS and RULES together would stop emitting its metric, and a
    # hard-zero key that is never emitted is never compared -- the gate would
    # pass by having stopped asking the question.
    "sync_drift.unregistered_synced_table",
    "sync_drift.registered_table_missing",
    "sync_drift.version_ladder_mismatch",
    "sync_drift.undocumented_table_or_column",
    "sync_drift.naive_stamp_default",
    "sync_drift.mirror_version_mismatch",
    "sync_drift.migration_step_changed",
    "sync_drift.undeclared_state_table",
    "sync_drift.wire_shape_changed",
})

# Metrics that are measured and printed but never gated, because their value
# is not reproducible across hosts. A ratchet compares today's number to a
# number recorded on some other machine, so a metric that legitimately differs
# by platform can only ever produce false failures or a silently inflated
# allowance -- both of which end with someone switching the gate off.
#
# deps.issues is the only one. deptry splits an import into DEP001 (undeclared)
# or DEP003 (transitive) by looking at what is actually resolvable, and it
# scores a platform-gated declaration as DEP002 (unused) on the platform where
# the marker is false. Measured Mon 17 Aug 2026 on identical trees: macOS reads
# 22 (DEP003=12, DEP001=6, DEP002=4), ubuntu-latest reads 24 (DEP001=19,
# DEP002=5, the extra DEP002 being pyobjc-framework-Quartz behind
# `sys_platform == 'darwin'`). Every one of the other 22 metrics agreed exactly
# across the two hosts, so this is deptry's environment sensitivity, not noise
# in the gate.
#
# To re-gate it, make the measurement host-independent (run deptry in a pinned
# container, or record a per-platform baseline) rather than just deleting this
# entry.
#
# shell.files_scanned is the second, for the opposite reason: it is a CONTROL on
# the shell gate, not an allowance. It exists so a reader can see the four
# hard-zero shell metrics were measured over a real file set rather than over
# nothing, and gating it either way is wrong (a ratchet would fail on every new
# script added, a hard zero is nonsense). The floor that makes it meaningful is
# enforced in _eval_shell, which aborts rather than scoring an empty scan.
#
# mypy.files_checked is the third, and it is a control of the same kind: the
# number of modules the type checker actually looked at. Error counts fall
# when code improves AND when the scope shrinks, and only this metric tells
# the two apart. Gating it would be wrong both ways (a ratchet fails on every
# new module; a hard zero is nonsense), so the floor that makes it meaningful
# is enforced in _eval_mypy, which aborts rather than scoring a collapsed scan.
#
# sync_drift.checks_run is the fourth, and the same kind: it reports how many
# of the drift checks actually ran, so the hard zeros above cannot be read as
# clean when the truth is that nothing ran. What makes that number mean
# something is NOT the count itself (counting loop iterations always agrees
# with itself, and prints "7 of 7" after a check is deleted). It is the pair
# of declarations either side of it: scripts/sync_drift_lint.
# assert_checks_wired refuses to run when a check defined there is not
# dispatched, and _eval_sync_drift below refuses to return unless every
# hard-zero sync_drift key in this file was emitted.
REPORT_ONLY: frozenset[str] = frozenset({
    "deps.issues",
    "shell.files_scanned",
    "mypy.files_checked",
    "sync_drift.checks_run",
})


@dataclass(frozen=True)
class Metric:
    key: str
    value: float
    unit: str
    detail: str = ""


@dataclass(frozen=True)
class HotspotResult:
    rows: list[tuple[str, int, int, int]]
    status: str = "PASS"
    detail: str = ""


@dataclass(frozen=True)
class MeasurementFailure:
    dimension: str
    detail: str


@dataclass
class Evaluator:
    name: str
    title: str
    run: Callable[[], list[Metric]]
    needs_node: bool = False
    notes: list[str] = field(default_factory=list)


# ----- process helpers -----------------------------------------------------


def _run_capture(
    cmd: list[str],
    cwd: Path = REPO,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """Run a command and retain both streams for explicit UNKNOWN results."""
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False, env=env)
    return proc.returncode, proc.stdout, proc.stderr


def _run(
    cmd: list[str],
    cwd: Path = REPO,
    allow_fail: bool = False,
    env: dict[str, str] | None = None,
) -> tuple[int, str]:
    """Run a command, returning (exit code, stdout). Fails fast by default.

    `env=None` inherits the caller's environment unchanged, matching
    `subprocess.run`'s own default. Pass a full replacement dict (built from
    `os.environ`, not a bare override) to strip specific variables.
    """
    returncode, stdout, stderr = _run_capture(cmd, cwd=cwd, env=env)
    if returncode != 0 and not allow_fail:
        raise RuntimeError(
            f"{' '.join(cmd[:4])}... exited {returncode}\n"
            f"stdout: {stdout[-2000:]}\nstderr: {stderr[-2000:]}"
        )
    return returncode, stdout


def _uv(
    *args: str,
    allow_fail: bool = False,
    reqs: Path = TOOL_REQS,
    isolated: bool = False,
) -> tuple[int, str]:
    """Run a pinned Python tool in a throwaway env, never the project venv.

    `isolated` is load bearing for any tool that RESOLVES IMPORTS, and it is
    not what `--no-project` does. `--no-project` declines to sync the project,
    but uv still discovers a `.venv` in the working directory and layers the
    `--with-requirements` packages ON TOP of it. Measured Tue 1 Sep 2026: the
    same tree read 550 mypy errors from a worktree holding a `.venv` and 1356
    from one without, because `pytest`, `fastapi`, `pydantic` and `numpy`
    resolved in the first and not the second. `--isolated` is what actually
    makes the env hold the pinned manifest and nothing else.

    Left off elsewhere on purpose. ruff and radon parse rather than resolve, so
    they cannot see a venv; import-linter scores only first-party modules. The
    one other import-resolving tool is deptry, and `deps.issues` is already
    REPORT_ONLY for exactly this class of reason -- flipping its environment
    would move that number for reasons unrelated to the tree, which is a
    separate decision from this one.

    `--isolated` pins the PACKAGE set; it does not touch the process
    environment `uv run` forwards into the tool. `isolated=True` also strips
    `IMPORT_PATH_ENV_VARS` from that forwarded environment, because mypy reads
    MYPYPATH (and PYTHONPATH) directly to widen its own search path outside
    uv's env entirely -- see the module-level comment on that constant for the
    measured 107 -> 117 swing from one inert ambient variable.

    `--no-env-file` closes the same hole one layer up: uv itself reads a
    `.env` file (path from `UV_ENV_FILE`, `.env` by default) and injects ITS
    contents into the child, which happens after the stripped `env` below is
    handed to `_run` and so bypasses it entirely. Confirmed the same
    107 -> 117 swing via `UV_ENV_FILE` pointing at a file that just sets
    MYPYPATH, with both variables absent from the parent process.
    """
    if not reqs.exists():
        raise RuntimeError(f"missing pinned tool manifest: {reqs}")
    cmd = [
        "uv", "run", "--no-project", "--quiet",
        *(["--isolated", "--no-env-file"] if isolated else []),
        "--with-requirements", str(reqs),
        *args,
    ]
    env = None
    if isolated:
        env = {k: v for k, v in os.environ.items() if k not in IMPORT_PATH_ENV_VARS}
    return _run(cmd, allow_fail=allow_fail, env=env)


def _pnpm_dlx(pkg: str, *args: str, allow_fail: bool = False) -> tuple[int, str]:
    return _run(["pnpm", "dlx", pkg, *args], cwd=FRONTEND, allow_fail=allow_fail)


def _is_vendored(rel: str) -> bool:
    return any(rel.startswith(v) for v in CFG.VENDORED)


def _is_derived(rel: str) -> bool:
    return any(rel.startswith(d) for d in CFG.DERIVED)


def _python_files() -> list[Path]:
    out: list[Path] = []
    for root in CFG.PY_PATHS:
        for path in (REPO / root).rglob("*.py"):
            rel = path.relative_to(REPO).as_posix()
            if "__pycache__" in rel or _is_vendored(rel) or _is_derived(rel):
                continue
            out.append(path)
    return out


# TypeScript constructs that turn the compiler off for a line, a file, or a
# value. Hard zero for the same reason as the shell rules above: a suppressed
# error is not a smaller error, it is an unmeasured one, so "we are allowed
# four of them" is not a position anyone would defend out loud. The Tue 1 Sep
# 2026 typing audit measured the tree at zero of every rule below; this is
# what keeps it there.
#
# Both the suppression directives (@ts-ignore/@ts-expect-error/@ts-nocheck)
# and explicit `any` used to be matched here in Python, the first by raw-line
# regex and the second by token-adjacency regex. Review of PR #731 found gaps
# in both, in the same shape: `type X = [any]` and friends landed green
# because adjacency regex cannot enumerate every syntactic position a grammar
# allows, and `const help = "Never use @ts-ignore here";` counted as a
# suppression because a raw-line regex cannot tell a string literal from a
# comment. Both are now found by one real parse in
# apps/webui/frontend/scripts/ts-any-scan.mjs, which also parses `.svelte`
# files with `svelte/compiler` itself so a markup-embedded `any`
# (`on:click={(e) => e.target as any}`) is found the same way a script-block
# one is, instead of only ever reading `<script>` text.
_TS_ANY_SCAN = FRONTEND / "scripts" / "ts-any-scan.mjs"


def _ts_scan_hits(files: list[Path]) -> dict[Path, list[tuple[int, str]]]:
    """Run the real parser over `files`, once, batched.

    Returns (line, rule) pairs per file for every `any` and every compiler
    suppression directive. A failed parse must not render as zero hits
    (verification.md: a tool that cannot measure must report UNKNOWN, never a
    verdict), so a parse error in any file aborts the whole run via `_run`'s
    default fail-fast rather than silently reporting that file clean.
    """
    if not files:
        return {}
    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8"
    ) as fh:
        json.dump([str(p) for p in files], fh)
        manifest = Path(fh.name)
    try:
        _, out = _run(["node", str(_TS_ANY_SCAN), str(manifest)], cwd=FRONTEND)
    finally:
        manifest.unlink(missing_ok=True)
    result = json.loads(out)
    if result["filesScanned"] != len(files):
        raise RuntimeError(
            f"ts-any-scan.mjs scanned {result['filesScanned']} of {len(files)} "
            "files given; a silent drop would report a false clean"
        )
    if result["svelteFilesTotal"] > 0 and result["svelteFilesWithScript"] == 0:
        raise RuntimeError(
            "ts-any-scan.mjs found a <script> block in none of the .svelte "
            "files scanned; that is a broken extraction reporting as a "
            "clean tree, not a project with no script content"
        )
    by_file: dict[Path, list[tuple[int, str]]] = collections.defaultdict(list)
    for hit in result["hits"]:
        by_file[Path(hit["file"])].append((hit["line"], hit["rule"]))
    return by_file


def _ts_escape_hatch_hits(source: str, suffix: str = ".ts") -> list[tuple[int, str]]:
    """(line number, rule) for every compiler suppression in one source file.

    `source` is written to a real probe file and run through the actual
    parser, so a unit test exercises the same code path as the live gate
    rather than an approximation of it.
    """
    with tempfile.NamedTemporaryFile(
        "w", suffix=suffix, delete=False, encoding="utf-8"
    ) as fh:
        fh.write(source)
        probe = Path(fh.name)
    try:
        hits = _ts_scan_hits([probe])
    finally:
        probe.unlink(missing_ok=True)
    return sorted(hits.get(probe, []))


def _ts_escape_hatches() -> tuple[int, str]:
    """Count suppressions across every hand-written frontend source, worst named."""
    files = _ts_project_files()
    if not files:
        raise RuntimeError(
            "frontend escape-hatch scan found no .ts/.svelte files; that is a "
            "broken scan reporting as a clean tree"
        )
    hits: list[str] = []
    for path, file_hits in _ts_scan_hits(files).items():
        rel = path.relative_to(REPO).as_posix()
        hits += [f"{rel}:{lineno} {rule}" for lineno, rule in file_hits]
    return len(hits), "; ".join(hits[:3])


def _frontend_files() -> list[Path]:
    src = FRONTEND / "src"
    # api-types.ts is emitted by openapi-typescript ("Do not make direct
    # changes"); its length tracks the API surface, not hand-written bloat,
    # so sizing it would gate every new endpoint. Same principle as keeping
    # the desktop payload out of the Python scans.
    generated = {src / "lib" / "api-types.ts"}
    return [
        p
        for p in src.rglob("*")
        if p.suffix in {".ts", ".svelte", ".js"} and p.is_file() and p not in generated
    ]


# `x as unknown as T` is the one assertion TypeScript cannot argue with: a
# single `as` still demands that the two types overlap, while routing through
# `unknown` erases the source type first and asserts anything onto anything.
# That is why the frontend's hand-written response interfaces could drift from
# the daemon's generated schemas without a single compiler error -- one of them
# promised a non-optional `PlaylistDetail.updated_at` the server never sent.
# Ratcheted, not hard-zero: 16 remain outside src/lib/api.ts as of Tue 1 Sep
# 2026, each one its own conversion, and the target is 0.
# `\s+` and not a literal space: a formatter is free to wrap the assertion
# across two lines, and a whitespace-literal pattern would let it hide there.
_FE_UNKNOWN_CAST_RE = re.compile(r"\bas\s+unknown\s+as\b")


def _unknown_cast_count(source: str) -> int:
    """`as unknown as` occurrences in one source file."""
    return len(_FE_UNKNOWN_CAST_RE.findall(source))


def _fe_unknown_casts() -> tuple[int, str]:
    """Count `as unknown as` double-casts over the frontend, worst file named."""
    files = _frontend_files()
    if not files:
        raise RuntimeError(
            "frontend double-cast scan found no .ts/.svelte files; that is a "
            "broken scan reporting as a clean tree"
        )
    per_file: collections.Counter[str] = collections.Counter()
    for path in files:
        hits = _unknown_cast_count(path.read_text(encoding="utf-8", errors="replace"))
        if hits:
            per_file[path.relative_to(REPO).as_posix()] = hits
    detail = ", ".join(f"{rel}={n}" for rel, n in per_file.most_common(3))
    return sum(per_file.values()), detail


# Directories under apps/webui/frontend that are generated or vendored, per
# apps/webui/frontend/.gitignore. Excluded so the scan below stays a scan of
# hand-written, compiler-checked sources rather than build output.
_TS_PROJECT_EXCLUDED_DIRS: frozenset[str] = frozenset(
    {
        "node_modules",
        "build",
        ".svelte-kit",
        ".vite",
        "storybook-static",
        "test-results",
        "playwright-report",
    }
)


def _ts_project_files() -> list[Path]:
    """Every hand-written TS/JS/Svelte file the frontend project compiles.

    `_frontend_files()` only walks src/, so config and tooling files that
    TypeScript checks just as strictly -- vite.config.ts, webui-port-config.ts,
    .storybook/*.ts, tests/e2e/playwright.performance.config.ts -- were never
    scanned for escape hatches. This walks the whole frontend project instead,
    excluding only generated/vendored directories, so an `any` planted outside
    src/ is caught the same as one inside it.
    """
    generated = {FRONTEND / "src" / "lib" / "api-types.ts"}
    return [
        p
        for p in FRONTEND.rglob("*")
        if p.suffix in {".ts", ".svelte", ".js"}
        and p.is_file()
        and p not in generated
        and _TS_PROJECT_EXCLUDED_DIRS.isdisjoint(p.relative_to(FRONTEND).parts)
    ]


# ----- evaluator: ruff -----------------------------------------------------

# Rule-code prefix to the question it answers. Order matters: first match wins,
# so the longer prefixes are listed before their parents.
_RUFF_BUCKETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("complexity", ("C901", "PLR0911", "PLR0912", "PLR0913", "PLR0915", "PLR0904")),
    # TID252 deliberately absent: see the note in pyproject.toml [tool.ruff.lint].
    ("coupling", ("ARG", "PLW0603")),
    ("safety", ("BLE", "TRY", "DTZ", "B9", "PLW1510")),
    ("correctness", ("F", "B", "RUF", "PIE", "RET")),
    ("style", ("E", "W", "I", "UP", "SIM", "PERF", "PL")),
)


def _ruff_bucket(code: str) -> str:
    for bucket, prefixes in _RUFF_BUCKETS:
        if any(code.startswith(p) for p in prefixes):
            return bucket
    return "style"


def _eval_ruff() -> list[Metric]:
    _, out = _uv(
        "ruff", "check", *CFG.PY_LINT_PATHS,
        "--output-format", "json", "--no-cache",
        allow_fail=True,
    )
    findings = json.loads(out)
    by_bucket: collections.Counter[str] = collections.Counter()
    by_code: collections.Counter[str] = collections.Counter()
    for f in findings:
        code = f.get("code") or "UNKNOWN"
        by_code[code] += 1
        by_bucket[_ruff_bucket(code)] += 1
    metrics = [Metric("ruff.total", len(findings), "violations",
                      f"top: {', '.join(f'{c}={n}' for c, n in by_code.most_common(3))}")]
    for bucket, _ in _RUFF_BUCKETS:
        worst = [c for c in by_code if _ruff_bucket(c) == bucket]
        worst.sort(key=lambda c: -by_code[c])
        metrics.append(
            Metric(
                f"ruff.{bucket}",
                by_bucket[bucket],
                "violations",
                ", ".join(f"{c}={by_code[c]}" for c in worst[:3]),
            )
        )
    return metrics


# ----- evaluator: complexity ----------------------------------------------


def _eval_complexity() -> list[Metric]:
    paths = ["apps"]
    # radon walks the tree itself and does NOT read .gitignore, so the desktop
    # payload's bundled CPython has to be excluded by hand. Skipping it in the
    # loop below is not enough: radon reports an unparseable file as an error
    # entry, and numpy ships .pxd files that are not Python, so an unexcluded
    # payload crashes this evaluator before the filter is ever reached.
    excludes = ",".join(f"{prefix}*" for prefix in CFG.DERIVED)
    _, cc_raw = _uv("radon", "cc", *paths, "-e", excludes, "-j", allow_fail=True)
    cc = json.loads(cc_raw)
    worst_name, worst_value = "", 0
    over_limit = 0
    for file, blocks in cc.items():
        if _is_vendored(file) or _is_derived(file):
            continue
        if isinstance(blocks, dict) and blocks.get("error"):
            raise RuntimeError(f"radon failed to parse {file}: {blocks['error']}")
        for block in blocks:
            value = block["complexity"]
            if value > CFG.MAX_COMPLEXITY:
                over_limit += 1
            if value > worst_value:
                worst_value = value
                worst_name = f"{file}:{block['lineno']} {block['name']}"

    _, mi_raw = _uv("radon", "mi", *paths, "-e", excludes, "-j", allow_fail=True)
    mi = json.loads(mi_raw)
    low_mi = sorted(
        (
            (file, data["mi"])
            for file, data in mi.items()
            if isinstance(data, dict) and "rank" in data
            and data["rank"] != "A"
            and not _is_vendored(file)
            and not _is_derived(file)
        ),
        key=lambda kv: kv[1],
    )
    return [
        Metric("complexity.worst_block", worst_value, "cyclomatic", worst_name),
        Metric("complexity.blocks_over_limit", over_limit, f"blocks > CC {CFG.MAX_COMPLEXITY}"),
        Metric(
            "complexity.low_maintainability_files",
            len(low_mi),
            "files below MI rank A",
            f"worst: {low_mi[0][0]} (MI {low_mi[0][1]:.1f})" if low_mi else "",
        ),
    ]


# ----- evaluator: architecture --------------------------------------------


def _eval_arch() -> list[Metric]:
    code, out = _uv("lint-imports", "--config", ".importlinter", allow_fail=True)
    match = re.search(r"Contracts:\s+(\d+) kept,\s+(\d+) broken", out)
    if not match:
        raise RuntimeError(f"could not parse import-linter output:\n{out[-2000:]}")
    kept, broken = int(match.group(1)), int(match.group(2))
    if broken == 0 and code != 0:
        raise RuntimeError(f"import-linter exited {code} with 0 broken contracts:\n{out[-2000:]}")

    _, cycles_raw = _uv("python", "-c", _PACKAGE_CYCLE_PROBE)
    cycles = json.loads(cycles_raw)
    return [
        Metric("arch.contracts_broken", broken, "contracts", f"{kept} kept"),
        Metric(
            "python.package_cycles",
            len(cycles),
            "mutually importing package pairs",
            ", ".join(f"{a} <-> {b}" for a, b in cycles),
        ),
    ]


_PACKAGE_CYCLE_PROBE = """
import grimp, itertools, json, collections
g = grimp.build_graph('apps', include_external_packages=False)
def top(m):
    parts = m.split('.')
    return parts[1] if len(parts) > 1 else None
edges = set()
for m in g.modules:
    a = top(m)
    if not a:
        continue
    for imported in g.find_modules_directly_imported_by(m):
        b = top(imported)
        if b and b != a:
            edges.add((a, b))
tops = sorted({t for e in edges for t in e})
cycles = [[a, b] for a, b in itertools.combinations(tops, 2)
          if (a, b) in edges and (b, a) in edges]
print(json.dumps(cycles))
"""


# ----- evaluator: dependencies --------------------------------------------


def _eval_deps() -> list[Metric]:
    # deptry writes its human report to stdout and structured findings to the
    # --json-output path, so read the file rather than parsing the console.
    #
    # Fresh mkdtemp per call, for the reason spelled out on `_eval_types`, plus
    # one a hosted runner never showed: /tmp survives the job on a self-hosted
    # runner and is sticky, so a file left at a fixed path by a DIFFERENT user
    # cannot be rewritten or even unlinked, and the gate dies on the leftover.
    #
    # Removed in a `finally`, like `_eval_mypy`'s, and for the other half of the
    # same fact: a persistent runner does not tidy /tmp when the job ends, so a
    # per-run directory that is never removed trades one leftover for unbounded
    # many. Codex caught this on PR #1014 (P2, discussion_r3923714170).
    report_dir = Path(tempfile.mkdtemp(prefix="quality-gate-deptry-"))
    out_path = report_dir / "deptry.json"
    try:
        # ROOT must be "." not "apps": given "apps" deptry treats the package's own
        # modules as third party and reports 400+ phantom DEP001s for `import apps.x`.
        # Scope and name mapping live in [tool.deptry] in pyproject.toml.
        _uv("deptry", ".", "--json-output", str(out_path), allow_fail=True)
        if not out_path.exists():
            raise RuntimeError("deptry produced no JSON output; the invocation is wrong")
        issues = json.loads(out_path.read_text())
    finally:
        shutil.rmtree(report_dir, ignore_errors=True)
    by_code: collections.Counter[str] = collections.Counter(i["error"]["code"] for i in issues)
    return [
        Metric(
            "deps.issues",
            len(issues),
            "dependency defects",
            ", ".join(f"{c}={n}" for c, n in by_code.most_common()),
        )
    ]


# ----- evaluator: mypy -----------------------------------------------------

# The metrics that own the scored roots. Errors are split rather than totalled
# because test-fixture debt and production debt are not the same debt: one
# `mypy.errors` would let 40 new errors in apps/ hide behind 40 deleted test
# modules, which is precisely the regression a ratchet exists to catch.
_MYPY_ERROR_BUCKETS: tuple[str, ...] = ("apps", "tests", "scripts")


def _mypy_bucket(rel: str) -> str:
    """Map a reported file to the metric that owns it, or refuse to guess."""
    head = rel.split("/", 1)[0]
    # conftest.py is the one scored root that is a file. It is test scaffolding,
    # so its debt is test debt rather than a fourth metric holding one number.
    if head == "conftest.py":
        return "tests"
    if head in _MYPY_ERROR_BUCKETS:
        return head
    raise RuntimeError(
        f"mypy reported an error in {rel}, which no metric owns. Either "
        f"[tool.mypy] files in pyproject.toml gained a root that "
        f"_MYPY_ERROR_BUCKETS does not name, or mypy followed an import out of "
        "the scored tree. Silently dropping it would understate the debt."
    )


def _mypy_metrics(records: list[dict[str, Any]], files_checked: int) -> list[Metric]:
    """Turn mypy's JSON diagnostics into one metric per scored root."""
    by_bucket: collections.Counter[str] = collections.Counter(
        dict.fromkeys(_MYPY_ERROR_BUCKETS, 0)
    )
    by_code: dict[str, collections.Counter[str]] = {
        bucket: collections.Counter() for bucket in _MYPY_ERROR_BUCKETS
    }
    for record in records:
        # mypy emits `note` severity for unchecked annotations and for the
        # follow-up lines of a multi-part diagnostic. Its own "Found N errors"
        # summary counts neither, and neither does this.
        if record["severity"] != "error":
            continue
        rel = record["file"]
        # tests/ imports ops.agentic_testing; mypy follows those imports out of
        # [tool.mypy] files (apps, tests, scripts). Scoring them as test debt
        # inflates mypy.errors_tests past the ceiling; aborting hides every
        # other metric. Drop follow-import hits from the unscored ops/ tree.
        if rel.split("/", 1)[0] == "ops":
            continue
        bucket = _mypy_bucket(rel)
        by_bucket[bucket] += 1
        by_code[bucket][record.get("code") or "unknown"] += 1

    metrics = [
        Metric(
            f"mypy.errors_{bucket}",
            by_bucket[bucket],
            "type errors",
            ", ".join(f"{c}={n}" for c, n in by_code[bucket].most_common(3)),
        )
        for bucket in _MYPY_ERROR_BUCKETS
    ]
    metrics.append(
        Metric(
            "mypy.files_checked",
            files_checked,
            "modules mypy checked",
            "control: the three counts above are measured over this set",
        )
    )
    return metrics


def _eval_mypy() -> list[Metric]:
    """Python type debt, measured under a pinned install set.

    The scope comes from `[tool.mypy] files` in pyproject.toml and the tool
    from ops/quality/mypy-requirements.txt, which holds mypy and nothing else.
    Both matter: an unresolvable third-party import scores as an error, so a
    count taken against whatever happened to be installed would move with the
    host rather than with the tree. That is the failure mode `deps.issues` had
    to be un-gated for.

    See `_uv` for why this call is `isolated=True`. Without it the measurement
    silently includes any `.venv` sitting in the working directory, which is a
    difference of 550 errors against 1356 on one identical commit -- caught by
    CI reading a number no developer machine could reproduce.

    The report directory is a fresh `tempfile.mkdtemp()` per call, not a fixed
    path. Two quality gates running at once (e.g. separate worktrees on one
    host) would otherwise share one process-global directory, each wiping the
    other's `--linecount-report` output mid-run.
    """
    from scripts.mypy_cache import mypy_cache_dir_flag

    cache_dir = mypy_cache_dir_flag()
    report_dir = Path(tempfile.mkdtemp(prefix="quality-gate-mypy-"))
    try:
        code, out = _uv(
            "mypy",
            *CFG.MYPY_FLAGS,
            "--cache-dir",
            cache_dir,
            "--linecount-report",
            str(report_dir),
            allow_fail=True,
            reqs=MYPY_REQS,
            isolated=True,
        )
        # 0 = clean, 1 = type errors found. Anything else is mypy declining to
        # run at all (bad config, unreadable source), which has to abort: a
        # crashed checker reporting zero type errors is the worst of the three.
        if code not in (0, 1):
            # mypy writes config and usage errors to stderr, and the call
            # above discarded it (allow_fail keeps only stdout, which holds
            # the JSON). Repeating the run without allow_fail costs nothing on
            # a path that is already aborting and makes _run raise with BOTH
            # streams attached, so the reader gets the actual complaint
            # instead of a bare exit code.
            _uv(
                "mypy",
                *CFG.MYPY_FLAGS,
                "--cache-dir",
                cache_dir,
                reqs=MYPY_REQS,
                isolated=True,
            )
            raise RuntimeError(f"mypy exited {code} on the scored run, then {out[-2000:]}")

        linecount = report_dir / "linecount.txt"
        if not linecount.exists():
            raise RuntimeError(f"mypy wrote no linecount report to {report_dir}")
        # One `total` row followed by one row per module mypy actually checked.
        files_checked = len(linecount.read_text(encoding="utf-8").splitlines()) - 1
        if files_checked < CFG.MYPY_MIN_FILES:
            raise RuntimeError(
                f"mypy checked {files_checked} modules, expected at least "
                f"{CFG.MYPY_MIN_FILES}. That is a collapsed scope reporting as a "
                "smaller pile of type errors."
            )

        records = [json.loads(line) for line in out.splitlines() if line.strip()]
        return _mypy_metrics(records, files_checked)
    finally:
        shutil.rmtree(report_dir, ignore_errors=True)


# ----- evaluator: frontend graph + knip ------------------------------------

_FE_IMPORT_RE = re.compile(
    r"""(?:import|export)\s(?:[^'"();]*?\sfrom\s)?['"]([^'"]+)['"]|import\(\s*['"]([^'"]+)['"]"""
)
_FE_SUFFIXES = (".ts", ".svelte", ".js", "/index.ts", "/index.js", "")


def _fe_resolve(spec: str, from_file: Path) -> Path | None:
    """Resolve a SvelteKit import specifier to a file inside src/, or None."""
    src = FRONTEND / "src"
    if spec.startswith("$lib/"):
        base = src / "lib" / spec[len("$lib/"):]
    elif spec == "$lib":
        base = src / "lib" / "index"
    elif spec.startswith("."):
        base = (from_file.parent / spec).resolve()
    else:
        return None  # $app/*, node builtins, npm packages
    for suffix in _FE_SUFFIXES:
        candidate = Path(str(base) + suffix)
        if candidate.is_file():
            return candidate
    return None


def _fe_graph() -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for path in _frontend_files():
        key = path.relative_to(FRONTEND).as_posix()
        deps: set[str] = set()
        for match in _FE_IMPORT_RE.finditer(path.read_text(encoding="utf-8", errors="replace")):
            spec = match.group(1) or match.group(2)
            resolved = _fe_resolve(spec, path)
            if resolved is not None and resolved != path:
                deps.add(resolved.relative_to(FRONTEND).as_posix())
        graph[key] = deps
    return graph


def _strongly_connected(graph: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan SCC. Any component of size > 1 is an import cycle."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    result: list[list[str]] = []
    counter = 0

    def strongconnect(node: str) -> None:
        nonlocal counter
        index[node] = low[node] = counter
        counter += 1
        stack.append(node)
        on_stack.add(node)
        for dep in graph.get(node, ()):
            if dep not in index:
                strongconnect(dep)
                low[node] = min(low[node], low[dep])
            elif dep in on_stack:
                low[node] = min(low[node], index[dep])
        if low[node] == index[node]:
            component = []
            while True:
                popped = stack.pop()
                on_stack.discard(popped)
                component.append(popped)
                if popped == node:
                    break
            if len(component) > 1:
                result.append(sorted(component))

    sys.setrecursionlimit(max(sys.getrecursionlimit(), 10_000))
    for node in graph:
        if node not in index:
            strongconnect(node)
    return result


def _eval_frontend() -> list[Metric]:
    graph = _fe_graph()
    edges = sum(len(v) for v in graph.values())
    if edges == 0:
        raise RuntimeError("frontend import graph is empty; the resolver is broken")
    cycles = _strongly_connected(graph)
    fan_in: collections.Counter[str] = collections.Counter()
    for deps in graph.values():
        for dep in deps:
            fan_in[dep] += 1
    fan_out = collections.Counter({k: len(v) for k, v in graph.items()})

    _run(["pnpm", "exec", "svelte-kit", "sync"], cwd=FRONTEND, allow_fail=True)
    _, knip_raw = _pnpm_dlx(CFG.KNIP, "--reporter", "json", allow_fail=True)
    knip = json.loads(knip_raw[knip_raw.index("{"):])
    unused_files = [i["file"] for i in knip["issues"] if i.get("files")]
    unused_exports = sum(len(i.get("exports", [])) + len(i.get("types", []))
                         for i in knip["issues"])
    unused_deps = sum(len(i.get("dependencies", [])) + len(i.get("devDependencies", []))
                      for i in knip["issues"])
    casts, cast_detail = _fe_unknown_casts()

    hatches, hatch_detail = _ts_escape_hatches()

    return [
        Metric("frontend.ts_escape_hatches", hatches, "compiler suppressions",
               hatch_detail),
        Metric(
            "frontend.import_cycles",
            len(cycles),
            "cyclic module groups",
            "; ".join(" <-> ".join(c) for c in cycles[:2]),
        ),
        Metric("frontend.max_fan_in", fan_in.most_common(1)[0][1] if fan_in else 0,
               "importers of one module",
               fan_in.most_common(1)[0][0] if fan_in else ""),
        Metric("frontend.max_fan_out", fan_out.most_common(1)[0][1] if fan_out else 0,
               "imports from one module",
               fan_out.most_common(1)[0][0] if fan_out else ""),
        Metric("frontend.unused_files", len(unused_files), "orphaned modules",
               ", ".join(unused_files[:3])),
        Metric("frontend.unused_exports", unused_exports, "unreferenced exports"),
        Metric("frontend.unused_deps", unused_deps, "unreferenced package.json deps"),
        Metric("frontend.unknown_casts", casts, "`as unknown as` double-casts",
               cast_detail),
    ]


# ----- evaluator: size + duplication ---------------------------------------


def _jscpd_duplication(apps_root: Path) -> Metric:
    """Run pinned jscpd over `apps_root` and return the duplication metric.

    Fresh report directory per call, removed after, for the same reason as
    the deptry report: two concurrent gates must not share a path. A missing
    JSON report is a broken tool, not a clean tree.
    """
    jscpd_dir = Path(tempfile.mkdtemp(prefix="quality-gate-jscpd-"))
    try:
        _pnpm_dlx(
            CFG.JSCPD,
            "--reporters", "json", "--output", str(jscpd_dir), "--silent",
            "--min-lines", str(CFG.DUP_MIN_LINES), "--min-tokens", str(CFG.DUP_MIN_TOKENS),
            "--ignore", ",".join(CFG.LOCKFILE_GLOBS + CFG.GENERATED_CONTRACT_GLOBS),
            str(apps_root),
            allow_fail=True,
        )
        report_path = jscpd_dir / "jscpd-report.json"
        if not report_path.is_file():
            raise RuntimeError(
                f"jscpd produced no JSON report at {report_path}; "
                "the pinned tool is missing, broken, or the scan root is empty"
            )
        report = json.loads(report_path.read_text())
    finally:
        shutil.rmtree(jscpd_dir, ignore_errors=True)
    total = report["statistics"]["total"]
    percent = round(float(total["percentage"]), 2)
    clones = int(total["clones"])
    return Metric(
        "duplication.percent", percent, "% duplicated lines", f"{clones} clones"
    )


def _eval_size() -> list[Metric]:
    def _measure(files: list[Path], limit: int) -> tuple[int, str, int]:
        sizes = sorted(
            ((len(p.read_text(encoding="utf-8", errors="replace").splitlines()), p) for p in files),
            reverse=True,
        )
        if not sizes:
            raise RuntimeError("no files found to size; the path config is wrong")
        return sizes[0][0], sizes[0][1].relative_to(REPO).as_posix(), sum(
            1 for n, _ in sizes if n > limit
        )

    py_max, py_worst, py_over = _measure(_python_files(), CFG.PY_FILE_LIMIT)
    fe_max, fe_worst, fe_over = _measure(_frontend_files(), CFG.FE_FILE_LIMIT)

    return [
        Metric("file_size.max_python", py_max, "lines", py_worst),
        Metric("file_size.over_limit_python", py_over, f"files > {CFG.PY_FILE_LIMIT} lines"),
        Metric("file_size.max_frontend", fe_max, "lines", fe_worst),
        Metric("file_size.over_limit_frontend", fe_over, f"files > {CFG.FE_FILE_LIMIT} lines"),
        _jscpd_duplication(REPO / "apps"),
    ]


# ----- evaluator: shell constructs -----------------------------------------


def _eval_shell() -> list[Metric]:
    """Four shell constructs that silently answer a question nobody asked.

    Hard zero, never a ratchet: every one of these returns a plausible value with
    no error, so "we are allowed four of them" is not a position anyone would
    defend out loud. Full rationale, the measured zsh modifier alphabet, and the
    mutation tests live in scripts/shell_construct_lint.py and
    tests/scripts/test_shell_construct_lint.py.
    """
    files = shell_construct_lint.discover(REPO)
    floor = shell_construct_lint.CFG.MIN_FILES
    if len(files) < floor:
        raise RuntimeError(
            f"shell lint discovered {len(files)} files under {REPO}, expected at "
            f"least {floor}. That is a broken scan reporting as a clean tree."
        )
    violations = shell_construct_lint.lint_paths(files)
    counts = collections.Counter(v.rule for v in violations)
    metrics = [
        Metric("shell.files_scanned", len(files), "shell files + justfiles",
               "control: the four gates below are measured over this set")
    ]
    for rule in sorted(shell_construct_lint.RULES):
        offenders = [v.render(REPO) for v in violations if v.rule == rule]
        metrics.append(
            Metric(
                f"shell.{rule.replace('-', '_')}",
                counts[rule],
                "violations",
                "; ".join(offenders[:3]),
            )
        )
    return metrics


# ----- evaluator: sync-schema drift ----------------------------------------


def _eval_sync_drift() -> list[Metric]:
    """Sync-schema omissions, measured against a production-shaped state DB.

    Hard zero, never a ratchet, for the same reason as the shell gate one
    section up and then some: these defects do not merely return a wrong
    answer, they return NO answer. A table missing from ``SYNC_TABLES`` does
    not fail to sync loudly, it simply never appears, and a ``SCHEMA_VERSION``
    that was not bumped makes ``apply_migrations`` skip the new DDL in silence.
    Neither has a runtime symptom to notice, so a growing allowance for them is
    a growing allowance for things nobody will ever find.

    Two floors, in two files, because one of them cannot see its own absence.
    ``sync_drift_lint.run`` raises rather than returning a clean result over a
    subject that was not really built or a check that was not dispatched; and
    the assertion at the end of this function raises when a rule named in
    HARD_ZERO stopped being emitted at all, which is what deleting a check
    looks like from out here. Rationale, the measured facts, and the dated
    allowlist entries live in scripts/sync_drift_lint.py,
    scripts/sync_drift_rules.py and tests/quality/test_sync_drift_lint.py.

    The linter is imported here rather than at module scope on purpose: it
    pulls in the application's schema modules, and a broken app module is
    exactly when someone reaches for ``--only ruff``.

    THIS IS THE ONLY EVALUATOR THAT IMPORTS THE APPLICATION, and that import
    has to stay inside what this gate's environment can satisfy: CI runs the
    whole file from a throwaway env holding ops/quality/requirements.txt and
    NOTHING of the project's own dependencies, deliberately. A module-level
    ``import yaml`` on the linter's path once killed the entire gate here
    with a ModuleNotFoundError naming PyYAML rather than drift -- every PR
    red, no report, no metrics. tests/quality/test_sync_drift_imports.py pins
    the invariant (the drift gate's import graph reaches no third party) so a
    reintroduction fails there by name instead of arriving as a red CI job
    about something else.
    """
    from scripts import sync_drift_lint

    with sync_drift_lint.measured_scan() as scan:
        result = sync_drift_lint.run(scan)
    counts = result.counts()
    metrics = [
        Metric(
            "sync_drift.checks_run",
            result.checks_run,
            f"of {len(sync_drift_lint.CHECKS)} checks completed",
            f"control: {result.tables_scanned} tables in the provisioned state DB, "
            f"{result.ladders_scanned} ladders built",
        )
    ]
    for rule in sorted(sync_drift_lint.RULES):
        offenders = [v.render() for v in result.violations if v.rule == rule]
        metrics.append(
            Metric(f"sync_drift.{rule}", counts[rule], "violations", "; ".join(offenders[:3]))
        )
    emitted = {m.key for m in metrics}
    silent = sorted(k for k in HARD_ZERO if k.startswith("sync_drift.") and k not in emitted)
    if silent:
        raise RuntimeError(
            f"the drift linter emitted no metric for {silent}. A hard-zero key "
            "that is never emitted is never compared, so the gate would pass "
            "by having stopped asking the question. Restore the check, or "
            "remove the key from HARD_ZERO deliberately."
        )
    return metrics


# ----- hotspots (report only) ----------------------------------------------


def _hotspots() -> HotspotResult:
    """Churn x size, or an explicit UNKNOWN when git cannot measure it."""
    code, log, stderr = _run_capture([
        "git", "log", f"--since={CFG.CHURN_DAYS}.days", "--no-renames",
        "--name-only", "--pretty=format:",
    ])
    if code != 0:
        detail = f"git log ... exited {code}; stderr: {stderr[-2000:]}"
        return HotspotResult([], "UNKNOWN", detail)
    churn: collections.Counter[str] = collections.Counter(
        line.strip() for line in log.splitlines() if line.strip()
    )
    rows: list[tuple[str, int, int, int]] = []
    for rel, commits in churn.items():
        path = REPO / rel
        if not path.is_file() or path.suffix not in {".py", ".ts", ".svelte"}:
            continue
        if _is_vendored(rel):
            continue
        lines = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
        rows.append((rel, commits, lines, commits * lines))
    rows.sort(key=lambda r: -r[3])
    return HotspotResult(rows[: CFG.HOTSPOT_COUNT])


def _eval_latency() -> list[Metric]:
    return [
        Metric(m["key"], float(m["value"]), str(m["unit"]), str(m.get("detail", "")))
        for m in quality_latency.evaluate(REPO)
    ]


# ----- registry ------------------------------------------------------------

EVALUATORS: tuple[Evaluator, ...] = (
    Evaluator("ruff", "Python lint debt by intent", _eval_ruff),
    Evaluator("complexity", "Python structural complexity", _eval_complexity),
    Evaluator("arch", "Architecture contracts and package cycles", _eval_arch),
    Evaluator("deps", "Dependency declaration defects", _eval_deps),
    Evaluator("mypy", "Python type debt by scored root", _eval_mypy),
    Evaluator("frontend", "Frontend coupling and dead code", _eval_frontend, needs_node=True),
    Evaluator("size", "File bloat and duplication", _eval_size, needs_node=True),
    Evaluator("shell", "Shell constructs that fail silently", _eval_shell),
    Evaluator("sync-drift", "Sync-schema omissions that never raise", _eval_sync_drift),
    Evaluator("latency", "Declared input-to-applied latency floor", _eval_latency),
)


# ----- ratchet -------------------------------------------------------------


def _load_baseline() -> dict[str, float]:
    if not BASELINE.exists():
        return {}
    return json.loads(BASELINE.read_text())["metrics"]


def _load_slack() -> dict[str, float]:
    """Per-metric headroom above the allowance, from baseline.json's `slack` (Q-12).

    A metric with no entry gets zero, which is the pre-slack gate exactly. The
    band therefore only ever exists where someone wrote it down and justified
    it in ops/quality/README.md; a missing key can never widen a gate quietly.
    """
    if not BASELINE.exists():
        return {}
    return json.loads(BASELINE.read_text()).get("slack", {})


def _ceiling(key: str, baseline: dict[str, float], slack: dict[str, float]) -> float:
    """The number a run must not exceed: allowance plus this metric's slack."""
    return baseline[key] + slack.get(key, 0.0)


def _allowed_text(key: str, baseline: dict[str, float], slack: dict[str, float]) -> str:
    """`49 allowed`, or `49 allowed + 1 slack` when the metric declares slack.

    The allowance and the slack print separately, never pre-added, so a reader
    can always see which number is the recorded debt and which is the band.
    """
    extra = slack.get(key, 0.0)
    if not extra:
        return f"{baseline[key]:g} allowed"
    return f"{baseline[key]:g} allowed + {extra:g} slack"


def _ratchet_exceeded(
    m: Metric, baseline: dict[str, float], slack: dict[str, float] | None = None
) -> bool:
    """True when a plain ratchet metric is over its allowance PLUS its slack.

    The only metrics a regression can be INHERITED on. A hard-zero rule has no
    allowance to be over on main (a broken architecture contract must stay red
    whoever caused it), and a report-only metric never regresses at all, so
    neither is ever offered the inherited downgrade. Slack defaults to empty,
    so a caller that does not pass one gets the strict allowance.
    """
    return (
        m.key not in HARD_ZERO
        and m.key not in REPORT_ONLY
        and m.key in baseline
        and m.value > _ceiling(m.key, baseline, slack or {})
    )


def _compare(
    metrics: list[Metric],
    baseline: dict[str, float],
    slack: dict[str, float] | None = None,
) -> tuple[list[str], list[str], list[str], list[str]]:
    """Split every metric into regressions, ratchets, unknowns and slack users.

    A value above the allowance but at or under allowance + slack is neither a
    failure nor a ratchet: it is one ordinary PR's worth of movement inside a
    declared band, reported as WITHIN SLACK with the raw numbers so it can
    never pass unseen.
    """
    slack_of = slack or {}
    regressions: list[str] = []
    ratchets: list[str] = []
    unknown: list[str] = []
    within_slack: list[str] = []
    for m in metrics:
        if m.key in HARD_ZERO:
            if m.value > 0:
                regressions.append(f"{m.key}: {m.value:g} (hard gate, must be 0)")
            continue
        if m.key in REPORT_ONLY:
            continue
        if m.key not in baseline:
            unknown.append(f"{m.key}: {m.value:g} (no baseline; run --update-baseline)")
        elif _ratchet_exceeded(m, baseline, slack_of):
            regressions.append(
                f"{m.key}: {m.value:g} > {_allowed_text(m.key, baseline, slack_of)}"
            )
        elif m.value > baseline[m.key]:
            within_slack.append(
                f"{m.key}: {m.value:g} > {baseline[m.key]:g} allowed, inside the "
                f"{slack_of.get(m.key, 0.0):g} slack "
                f"(ceiling {_ceiling(m.key, baseline, slack_of):g})"
            )
        elif m.value < baseline[m.key]:
            ratchets.append(f"{m.key}: {m.value:g} < {baseline[m.key]:g} allowed")
    return regressions, ratchets, unknown, within_slack


# ----- inherited trunk regressions (Q-11) ----------------------------------


@dataclass(frozen=True)
class BaseCheck:
    """Result of asking the merge-base main whether it is over too.

    sha is the short merge-base sha, "" when inheritance was undecidable.
    inherited maps a metric key to (metric, the value main measured).
    notes are the reasons any over-allowance metric was LEFT as a regression,
    so a failure always says why instead of silently passing.
    undecidable is True when the merge-base could not be resolved or measured
    at all; inheritance-eligible metrics must not print REGRESSION in that case.
    reason is the canonical UNKNOWN detail for base_compare when undecidable.
    """

    sha: str
    inherited: dict[str, tuple[Metric, float]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    undecidable: bool = False
    reason: str = ""


def _resolve_base() -> tuple[str | None, str]:
    """The merge-base sha with main, or (None, why) when undecidable.

    Inheritance is only meaningful when this run is not itself the trunk tip.
    On a push-to-main run HEAD is origin/main, so the "other main" a branch
    could blame is the very tree this run measures: downgrading there would
    silence the one run that exists to detect a regression landing on main.
    That case returns (None, ...) so the message stays exactly as it is today.
    """
    code, out = _run(["git", "merge-base", "HEAD", MAIN_BASE_REF], allow_fail=True)
    if code != 0:
        return None, f"git merge-base HEAD {MAIN_BASE_REF} failed (exit {code})"
    sha = out.strip()
    if not sha:
        return None, f"git merge-base HEAD {MAIN_BASE_REF} returned no commit"
    _, head_out = _run(["git", "rev-parse", "HEAD"])
    if sha == head_out.strip():
        return None, "HEAD is itself on main; there is no other main to inherit from"
    return sha, ""


def _measure_owners_at_base(
    sha: str, owners: list[str]
) -> tuple[dict[str, float] | None, str]:
    """Measure `owners` against the tree at `sha`; (metrics, "") or (None, why).

    The merge-base tree is checked out with `git worktree add --detach`, then
    the gate is re-run there with the base tree's OWN committed copy of
    scripts/quality_gate.py under the same ambient toolchain. Using the base's
    own gate matters twice over: a metric over `scripts/` must be measured
    against the base's version of that file, not this run's, and running the
    committed gate means the flag this branch added does not have to exist on
    main for the measurement to work. The base run writes its --json before it
    decides its own exit code, so a base that is itself red still yields its
    numbers.

    The cost is deliberate: only a run that already regressed pays for the
    second scan, and only for the evaluators that own the regressed metrics.
    The worktree lives in a throwaway tempdir and is removed in `finally`, so
    an interrupted run leaves at worst an orphaned entry that `git worktree
    prune` clears.
    """
    # mkdtemp creates the dir, which `git worktree add` refuses to reuse.
    base_dir = Path(tempfile.mkdtemp(prefix="quality-gate-base-"))
    base_dir.rmdir()
    try:
        code, stdout, stderr = _run_capture(
            ["git", "worktree", "add", "--detach", str(base_dir), sha],
        )
        if code != 0:
            detail = (stderr or stdout).strip()
            tail = detail[-400:] if detail else ""
            return None, (
                f"git worktree add of merge base {sha[:10]} failed (exit {code}): "
                f"{tail}"
            )
        if "frontend" in owners:
            # knip and svelte-kit resolve against a node_modules install, which
            # a git worktree does not carry. Reuse this run's install read-only
            # instead of running pnpm install on a throwaway tree.
            base_fe = base_dir / "apps" / "webui" / "frontend"
            if (FRONTEND / "node_modules").is_dir():
                (base_fe / "node_modules").symlink_to(
                    FRONTEND / "node_modules", target_is_directory=True
                )
        out_json = base_dir / "metrics.json"
        cmd = [
            sys.executable, "-m", "scripts.quality_gate",
            "--only", ",".join(owners), "--json", str(out_json),
        ]
        proc = subprocess.run(cmd, cwd=base_dir, capture_output=True, text=True, check=False)
        if not out_json.exists():
            # Base's own gate can abort before --json (mypy follow-import into
            # ops/agentic_testing/coach.py is the live case). Overlay this
            # run's gate onto the throwaway worktree and retry so trunk debt
            # can still inherit. The tree being measured stays the merge-base.
            dest = base_dir / "scripts" / "quality_gate.py"
            dest.write_text(Path(__file__).read_text(encoding="utf-8"), encoding="utf-8")
            proc = subprocess.run(cmd, cwd=base_dir, capture_output=True, text=True, check=False)
        if not out_json.exists():
            return None, (
                f"merge-base run for {','.join(owners)} exited {proc.returncode} "
                f"without metrics: {proc.stderr.strip()[-300:]}"
            )
        return json.loads(out_json.read_text()), ""
    finally:
        # Remove the worktree entry first so the shared .git does not accumulate
        # orphans, then clear any leftover files whether or not git agreed.
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(base_dir)],
            capture_output=True, text=True, check=False,
        )
        shutil.rmtree(base_dir, ignore_errors=True)


def _inherited_classification(
    over: list[Metric],
    owner_of: dict[str, str],
    resolve_base: Callable[[], tuple[str | None, str]],
    measure_owners: Callable[[str, list[str]], tuple[dict[str, float] | None, str]],
) -> BaseCheck:
    """Decide which over-allowance metrics the merge-base main already carries.

    A metric is inherited when main measured AT OR ABOVE this run's value: main
    is over the same allowance and this change added nothing to it, so failing
    the author for it blames them for trunk. A base BELOW this run's value means
    the change made an already-over metric worse - base at 50 and this run at 51
    is this change's fault - so it stays a hard REGRESSION. When the merge-base
    cannot be resolved or measured at all, inheritance is undecidable and the
    gate reports UNKNOWN (base_compare, exit 2), never REGRESSION, for plain
    ratchet metrics. A per-metric omission from an otherwise successful base run
    stays REGRESSION with a reason. HEAD-on-main is a measured regression, not
    undecidable.
    """
    head_on_main = "HEAD is itself on main; there is no other main to inherit from"
    if not over:
        return BaseCheck("")
    base_sha, reason = resolve_base()
    if base_sha is None:
        if reason == head_on_main:
            return BaseCheck("", {}, [
                f"cannot check the merge-base main: {reason}",
                f"{len(over)} regression(s) reported unqualified rather than guessed",
            ])
        canonical = f"cannot check the merge-base main: {reason}"
        return BaseCheck("", {}, undecidable=True, reason=canonical)
    short = base_sha[:10]
    owners = sorted({owner_of[m.key] for m in over})
    base_values, measure_reason = measure_owners(base_sha, owners)
    if base_values is None:
        canonical = (
            f"cannot re-measure {', '.join(owners)} on merge-base main {short}: "
            f"{measure_reason}"
        )
        return BaseCheck(short, {}, undecidable=True, reason=canonical)
    inherited: dict[str, tuple[Metric, float]] = {}
    notes: list[str] = []
    for m in over:
        base_value = base_values.get(m.key)
        if base_value is None:
            notes.append(f"{m.key}: the merge-base main run ({short}) did not "
                         "report it; left as a plain regression, not guessed")
        elif base_value >= m.value:
            inherited[m.key] = (m, base_value)
        else:
            notes.append(f"{m.key}: merge-base main ({short}) measured "
                         f"{base_value:g} against this run's {m.value:g}; this "
                         "change is worse than main, so it stays a regression")
    return BaseCheck(short, inherited, notes)


def _inherited_block(
    m: Metric,
    base_value: float,
    base_sha: str,
    baseline: dict[str, float],
    slack: dict[str, float] | None = None,
) -> tuple[str, str]:
    """The two console lines an INHERITED metric prints in place of REGRESSION."""
    return (
        f"INHERITED  {m.key}: {m.value:g} > "
        f"{_allowed_text(m.key, baseline, slack or {})}",
        f"          main ({base_sha}) is ALSO at {base_value:g} - this is a trunk "
        "regression, not yours",
    )


# ----- report --------------------------------------------------------------


def _report_allowed(
    key: str, baseline: dict[str, float], slack: dict[str, float]
) -> str:
    """The report's `allowed` cell: the allowance, and its slack band if any."""
    if key in HARD_ZERO:
        return "0 (hard)"
    if key in REPORT_ONLY:
        return "n/a"
    if key not in baseline:
        return "-"
    extra = slack.get(key, 0.0)
    if not extra:
        return f"{baseline[key]:g}"
    return f"{baseline[key]:g} (+{extra:g} slack)"


def _report_status(
    m: Metric,
    baseline: dict[str, float],
    slack: dict[str, float],
    inherited: frozenset[str],
) -> str:
    """The report's `status` cell for one metric.

    A regression the merge-base main already carries is not this change's
    doing, so INHERITED wins over REGRESSED; saying otherwise would put the
    status column in contradiction with the exit code.
    """
    if m.key in inherited:
        return "INHERITED (on main too)"
    if m.key in HARD_ZERO:
        return "PASS" if m.value == 0 else "FAIL"
    if m.key in REPORT_ONLY:
        return "report only"
    if m.key not in baseline:
        return "NEW"
    if m.value > _ceiling(m.key, baseline, slack):
        return "REGRESSED"
    if m.value > baseline[m.key]:
        return "within slack"
    if m.value < baseline[m.key]:
        return "RATCHET"
    return "held"


def _markdown(
    metrics: list[Metric],
    baseline: dict[str, float],
    hotspots: list,
    inherited: frozenset[str] = frozenset(),
    slack: dict[str, float] | None = None,
    measurement_failures: list[MeasurementFailure] | None = None,
) -> str:
    lines = [
        "# Code quality report",
        "",
        f"Generated {datetime.now(UTC).isoformat(timespec='seconds')} by "
        "`python -m scripts.quality_gate`.",
        "",
        "Every number is compared against `ops/quality/baseline.json`. Only a number",
        "past the allowance PLUS that metric's declared slack fails the build; a",
        "better number is a ratchet you can bank with `--update-baseline`.",
        "",
        "| metric | value | allowed | status | unit | worst offender |",
        "| ------ | ----: | ------: | ------ | ---- | -------------- |",
    ]
    slack_of = slack or {}
    for m in metrics:
        allowed = _report_allowed(m.key, baseline, slack_of)
        status = _report_status(m, baseline, slack_of, inherited)
        lines.append(
            f"| `{m.key}` | {m.value:g} | {allowed} | {status} | {m.unit} | {m.detail} |"
        )
    lines += [
        "",
        f"## Change hotspots (last {CFG.CHURN_DAYS} days)",
        "",
        "Churn multiplied by size. High-churn big files are where refactoring pays;",
        "a big file nobody edits is not urgent. Report only, never gated.",
        "",
        "| file | commits | lines | score |",
        "| ---- | ------: | ----: | ----: |",
    ]
    for rel, commits, size, score in hotspots:
        lines.append(f"| `{rel}` | {commits} | {size} | {score} |")
    if measurement_failures:
        lines += [
            "",
            "## Measurement status",
            "",
            "| dimension | status | detail |",
            "| --------- | ------ | ------ |",
        ]
        for failure in measurement_failures:
            lines.append(f"| `{failure.dimension}` | UNKNOWN | {failure.detail} |")
    return "\n".join(lines) + "\n"


def _write_trend_summary(trend_lines: list[str]) -> None:
    """Append main's non-blocking growth trend to the GitHub Actions job summary.

    No-ops outside CI (GITHUB_STEP_SUMMARY unset) so a local run is not affected.
    """
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if not summary_file or not trend_lines:
        return
    body = "\n".join(f"- {line}" for line in trend_lines)
    with Path(summary_file).open("a", encoding="utf-8") as handle:
        handle.write(
            "\n## quality ratchet: main trend report (non-blocking, issue #3246)\n\n"
            "These plain-ratchet metrics are above their `ops/quality/baseline.json` "
            "allowance on main. This is a REPORT, not a gate: the enforced check is the "
            "per-PR delta vs merge-base (see "
            "docs/decisions/ADR-NEW-quality-ratchet-new-code-gate.md).\n\n"
            f"{body}\n"
        )


# ----- main ----------------------------------------------------------------


def _preflight(selected: list[Evaluator]) -> None:
    if shutil.which("uv") is None:
        raise RuntimeError("uv is not on PATH; see CLAUDE.md (uv, never pip)")
    if any(e.needs_node for e in selected) and shutil.which("pnpm") is None:
        raise RuntimeError("pnpm is not on PATH but a frontend evaluator was selected")


def _marker(metric: Metric, allowed: float | None, slack: float = 0.0) -> str:
    """Two-character status flag for one metric line.

    `~~` is the slack band: over the recorded allowance, under the ceiling.
    It is deliberately not `  ` (held), so a reader can see at a glance that
    the tree floated up even though the run passed.
    """
    if metric.key in HARD_ZERO:
        return "!!" if metric.value > 0 else "OK"
    if metric.key in REPORT_ONLY:
        return "--"
    if allowed is None:
        return "??"
    if metric.value > allowed + slack:
        return "!!"
    if metric.value > allowed:
        return "~~"
    if metric.value < allowed:
        return "->"
    return "  "


def _print_metrics(
    metrics: list[Metric],
    baseline: dict[str, float],
    slack: dict[str, float] | None = None,
) -> None:
    print()
    slack_of = slack or {}
    for m in metrics:
        allowed = baseline.get(m.key)
        extra = slack_of.get(m.key, 0.0)
        if m.key in REPORT_ONLY:
            suffix = " (report only, not gated)"
        elif allowed is not None and extra:
            suffix = f" (allowed {allowed:g} + {extra:g} slack)"
        elif allowed is not None:
            suffix = f" (allowed {allowed:g})"
        else:
            suffix = ""
        detail = f"  [{m.detail}]" if m.detail else ""
        marker = _marker(m, allowed, extra)
        print(f" {marker:2s} {m.key:38s} {m.value:>8g} {m.unit}{suffix}{detail}")


def _recorded_allowances(
    metrics: list[Metric], previous: dict[str, float]
) -> tuple[dict[str, float], list[str]]:
    """The allowances a rewrite records: today's numbers, but only downward.

    --update-baseline must never RAISE an allowance, and slack is exactly what
    makes that possible to do by accident. A run inside its band measures above
    the recorded allowance and still passes (Q-12); writing that measurement
    back would bank the pass, and repeating it would walk the ceiling up one
    band per run -- the opposite of a ratchet, and a hole straight through the
    shrink-only invariant this file's own `note` states.

    So a metric measured ABOVE its recorded allowance keeps the old number and
    the run says which ones it kept. Only a lower measurement moves the file, a
    metric with no prior allowance is recorded as a first one, and REPORT_ONLY
    metrics are deliberately absent (this file lists allowances, and a number
    nothing is allowed to exceed is not one). Raising an allowance is still
    possible, and still exactly as visible as it was: hand-edit the number in a
    diff someone reviews, with a `burn_down` entry saying why.
    """
    recorded: dict[str, float] = {}
    retained: list[str] = []
    for m in metrics:
        if m.key in REPORT_ONLY:
            continue
        prior = previous.get(m.key)
        if prior is not None and m.value > prior:
            recorded[m.key] = prior
            retained.append(
                f"{m.key}: measured {m.value:g}, allowance kept at {prior:g}"
            )
        else:
            recorded[m.key] = m.value
    return recorded, retained


def _write_baseline(metrics: list[Metric]) -> None:
    BASELINE.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "generated": datetime.now(UTC).isoformat(timespec="seconds"),
        "note": "Allowances only ever shrink. See ops/quality/README.md.",
    }
    # Carry `burn_down` across rewrites. It names WHY a specific allowance was
    # raised and which refactor is committed to lowering it again. Dropping it
    # on the next --update-baseline would turn a tracked burn-down into an
    # anonymous number nobody remembers agreeing to.
    # `slack` is carried for the same reason: it is a reviewed, justified band
    # (ops/quality/README.md), and a --update-baseline that silently dropped it
    # would re-tighten seven gates to zero headroom without saying so.
    previous: dict[str, float] = {}
    if BASELINE.exists():
        existing = json.loads(BASELINE.read_text())
        previous = existing.get("metrics", {})
        for carried in ("slack", "burn_down"):
            if carried in existing:
                payload[carried] = existing[carried]
    payload["metrics"], retained = _recorded_allowances(metrics, previous)
    BASELINE.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"\n[quality] baseline written to {BASELINE}")
    for line in retained:
        print(f"[quality] ALLOWANCE KEPT   {line}")
    if retained:
        print(
            f"[quality] {len(retained)} allowance(s) were NOT raised to today's "
            "measurement. Allowances only ever shrink; slack is headroom at "
            "gate time, never a recorded number. To raise one, edit "
            "ops/quality/baseline.json by hand with a burn_down entry."
        )


def _select(only: str | None) -> list[Evaluator]:
    wanted = set(only.split(",")) if only else {e.name for e in EVALUATORS}
    unknown_names = wanted - {e.name for e in EVALUATORS}
    if unknown_names:
        raise SystemExit(f"unknown evaluator(s): {', '.join(sorted(unknown_names))}")
    return [e for e in EVALUATORS if e.name in wanted]


def _classify_regressions(
    regressions: list[str],
    check: BaseCheck,
    baseline: dict[str, float],
    slack: dict[str, float] | None,
    main_report_only: bool,
) -> tuple[int, list[str]]:
    """Print each regression line; split it into kept-vs-trend-only (issue #3246).

    An inherited metric prints INHERITED in place of REGRESSION and is counted
    as neither -- it already passed. Everything else prints REGRESSION: a
    HARD_ZERO gate always counts as kept (a correctness invariant, not a
    growth-sensitive count), and a plain-ratchet metric counts as kept unless
    main_report_only downgrades it to a non-blocking trend line. PR runs never
    pass main_report_only, so this only ever downgrades a main push/schedule
    run -- see ADR-NEW-quality-ratchet-new-code-gate.md.
    """
    regressions_kept = 0
    trend_only: list[str] = []
    for line in regressions:
        key = line.split(":", 1)[0]
        if key in check.inherited:
            m, base_value = check.inherited[key]
            line1, line2 = _inherited_block(m, base_value, check.sha, baseline, slack)
            print(f"[quality] {line1}")
            print(line2)
        elif check.undecidable and key not in HARD_ZERO:
            continue
        elif main_report_only and key not in HARD_ZERO:
            print(f"[quality] REGRESSION        {line}")
            trend_only.append(line)
        else:
            print(f"[quality] REGRESSION        {line}")
            regressions_kept += 1
    return regressions_kept, trend_only


def _print_ratchet_verdict(
    ratchets: list[str],
    unknown: list[str],
    regressions: list[str],
    check: BaseCheck,
    baseline: dict[str, float],
    within_slack: list[str] | None = None,
    slack: dict[str, float] | None = None,
    main_report_only: bool = False,
) -> int:
    """Print the ratchet/regression readout and return the run's exit code.

    An inherited metric prints INHERITED where its REGRESSION line would have
    been, so the distinction appears at the metric's own place in the list.
    Metric keys never contain a colon, so the line prefix names the metric.
    A metric inside its slack band prints WITHIN SLACK with both raw numbers:
    the run passes, but the movement is never invisible.
    Extracted from main so main stays under the gate's own complexity ceilings.
    """
    used_slack = within_slack or []
    print()
    for line in ratchets:
        print(f"[quality] RATCHET AVAILABLE  {line}")
    for line in unknown:
        print(f"[quality] NO BASELINE       {line}")
    for line in used_slack:
        print(f"[quality] WITHIN SLACK      {line}")
    regressions_kept, trend_only = _classify_regressions(
        regressions, check, baseline, slack, main_report_only
    )
    if not check.undecidable:
        for note in check.notes:
            print(f"[quality] base compare: {note}")
    if main_report_only and trend_only:
        _write_trend_summary(trend_only)
    if regressions_kept:
        print(f"\n[quality] FAIL: {regressions_kept} metric(s) got worse.")
        return 1
    if main_report_only and trend_only:
        print(
            f"\n[quality] TREND (non-blocking, main push/schedule -- issue #3246): "
            f"{len(trend_only)} metric(s) above the ops/quality/baseline.json allowance; "
            "the enforced gate is the per-PR delta vs merge-base, not this absolute count."
        )
        return 0
    if check.inherited:
        print(
            f"\n[quality] PASS: {len(check.inherited)} metric(s) over allowance "
            "are INHERITED from main (above); this change made none worse."
        )
        return 0
    if used_slack:
        print(
            f"\n[quality] PASS: {len(used_slack)} metric(s) used declared slack "
            "(above); none passed its ceiling."
        )
        return 0
    print("\n[quality] PASS: nothing got worse.")
    return 0


def _measurement_exit_code(
    ratchet_exit_code: int, failures: list[MeasurementFailure]
) -> int:
    """Keep measured FAIL dominant; otherwise distinguish UNKNOWN with exit 2."""
    if ratchet_exit_code == 1:
        return 1
    return 2 if failures else ratchet_exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--only", help="comma separated evaluator names")
    parser.add_argument("--update-baseline", action="store_true",
                        help="write current values as the new allowance")
    parser.add_argument("--report", type=Path, help="write a markdown report here")
    parser.add_argument("--json", type=Path, help="write raw metrics JSON here")
    parser.add_argument("--list", action="store_true", help="list evaluators and exit")
    parser.add_argument(
        "--main-report-only", action="store_true",
        help="on a main push/schedule CI run, print plain-ratchet regressions "
             "(baseline.json allowances) as a non-blocking trend report and exit 0; "
             "HARD_ZERO gates and PR merge-base regressions are unaffected (issue #3246)",
    )
    args = parser.parse_args(argv)

    if args.list:
        for e in EVALUATORS:
            print(f"{e.name:12s} {e.title}")
        return 0

    selected = _select(args.only)
    measurement_failures: list[MeasurementFailure] = []
    try:
        _preflight(selected)
    except RuntimeError as exc:
        measurement_failures.append(MeasurementFailure("preflight", str(exc)))

    metrics: list[Metric] = []
    owner_of: dict[str, str] = {}
    if not measurement_failures:
        for evaluator in selected:
            print(f"[quality] {evaluator.name}: {evaluator.title}", flush=True)
            try:
                measured = evaluator.run()
            except RuntimeError as exc:
                measurement_failures.append(MeasurementFailure(evaluator.name, str(exc)))
                print(f"[quality] UNKNOWN: {evaluator.name}: {exc}")
                continue
            # Record which evaluator owns each metric so an inherited re-measure
            # can re-run just that evaluator on the merge-base tree (Q-11).
            owner_of.update({m.key: evaluator.name for m in measured})
            metrics.extend(measured)

    baseline = _load_baseline()
    slack = _load_slack()
    regressions, ratchets, unknown, within_slack = _compare(metrics, baseline, slack)

    # Ask the merge-base main whether it is over too, but only when something
    # actually regressed: a green run must not pay for a second scan.
    check = BaseCheck("")
    if not args.update_baseline:
        over = [m for m in metrics if _ratchet_exceeded(m, baseline, slack)]
        check = _inherited_classification(
            over, owner_of, _resolve_base, _measure_owners_at_base
        )
        if check.undecidable:
            measurement_failures.append(
                MeasurementFailure("base_compare", check.reason)
            )
            print(f"[quality] UNKNOWN: base_compare: {check.reason}")

    hotspots = _hotspots()
    if hotspots.status == "UNKNOWN":
        measurement_failures.append(MeasurementFailure("hotspots", hotspots.detail))
        print(f"[quality] UNKNOWN: hotspots: {hotspots.detail}")

    _print_metrics(metrics, baseline, slack)

    # Absolute paths in the readout: this runs from justfile, Makefile and CI
    # with three different working directories, so a relative path is a path
    # the reader has to reconstruct before they can open it.
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            _markdown(
                metrics,
                baseline,
                hotspots.rows,
                inherited=frozenset(check.inherited),
                slack=slack,
                measurement_failures=measurement_failures,
            )
        )
        print(f"\n[quality] report written to {args.report.resolve()}")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {m.key: m.value for m in metrics}
        if measurement_failures:
            payload["measurements"] = {
                failure.dimension: {"status": "UNKNOWN", "error": failure.detail}
                for failure in measurement_failures
            }
        args.json.write_text(json.dumps(payload, indent=2) + "\n")
        print(f"[quality] metrics written to {args.json.resolve()}")

    if args.update_baseline:
        if measurement_failures:
            print("[quality] baseline not written because measurement is UNKNOWN")
            return 2
        if len(selected) != len(EVALUATORS):
            raise SystemExit("--update-baseline requires a full run (drop --only)")
        _write_baseline(metrics)
        return 0

    ratchet_exit_code = _print_ratchet_verdict(
        ratchets, unknown, regressions, check, baseline, within_slack, slack,
        main_report_only=args.main_report_only,
    )
    if measurement_failures:
        print(
            f"\n[quality] UNKNOWN: {len(measurement_failures)} measurement(s) "
            "could not be completed (exit 2)."
        )
    return _measurement_exit_code(ratchet_exit_code, measurement_failures)


if __name__ == "__main__":
    sys.exit(main())
