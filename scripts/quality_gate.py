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
REG  Q-08 Enforce three shell constructs as a hard gate (no ratchet) via
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

from scripts import shell_construct_lint

# ----- config --------------------------------------------------------------

REPO = Path(__file__).resolve().parent.parent
FRONTEND = REPO / "apps" / "webui" / "frontend"
TOOL_REQS = REPO / "ops" / "quality" / "requirements.txt"
MYPY_REQS = REPO / "ops" / "quality" / "mypy-requirements.txt"
BASELINE = REPO / "ops" / "quality" / "baseline.json"

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
# the shell gate, not an allowance. It exists so a reader can see the three
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
REPORT_ONLY: frozenset[str] = frozenset({
    "deps.issues",
    "shell.files_scanned",
    "mypy.files_checked",
})


@dataclass(frozen=True)
class Metric:
    key: str
    value: float
    unit: str
    detail: str = ""


@dataclass
class Evaluator:
    name: str
    title: str
    run: Callable[[], list[Metric]]
    needs_node: bool = False
    notes: list[str] = field(default_factory=list)


# ----- process helpers -----------------------------------------------------


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
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False, env=env)
    if proc.returncode != 0 and not allow_fail:
        raise RuntimeError(
            f"{' '.join(cmd[:4])}... exited {proc.returncode}\n"
            f"stdout: {proc.stdout[-2000:]}\nstderr: {proc.stderr[-2000:]}"
        )
    return proc.returncode, proc.stdout


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
        bucket = _mypy_bucket(record["file"])
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
    report_dir = Path(tempfile.mkdtemp(prefix="quality-gate-mypy-"))
    try:
        code, out = _uv(
            "mypy", *CFG.MYPY_FLAGS, "--linecount-report", str(report_dir),
            allow_fail=True, reqs=MYPY_REQS, isolated=True,
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
            _uv("mypy", *CFG.MYPY_FLAGS, reqs=MYPY_REQS, isolated=True)
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

    # Fresh per call AND removed after, both for the reasons on the deptry
    # report above.
    jscpd_dir = Path(tempfile.mkdtemp(prefix="quality-gate-jscpd-"))
    try:
        _pnpm_dlx(
            CFG.JSCPD,
            "--reporters", "json", "--output", str(jscpd_dir), "--silent",
            "--min-lines", str(CFG.DUP_MIN_LINES), "--min-tokens", str(CFG.DUP_MIN_TOKENS),
            str(REPO / "apps"),
            allow_fail=True,
        )
        report = json.loads((jscpd_dir / "jscpd-report.json").read_text())
    finally:
        shutil.rmtree(jscpd_dir, ignore_errors=True)
    percent = round(float(report["statistics"]["total"]["percentage"]), 2)
    clones = int(report["statistics"]["total"]["clones"])

    return [
        Metric("file_size.max_python", py_max, "lines", py_worst),
        Metric("file_size.over_limit_python", py_over, f"files > {CFG.PY_FILE_LIMIT} lines"),
        Metric("file_size.max_frontend", fe_max, "lines", fe_worst),
        Metric("file_size.over_limit_frontend", fe_over, f"files > {CFG.FE_FILE_LIMIT} lines"),
        Metric("duplication.percent", percent, "% duplicated lines", f"{clones} clones"),
    ]


# ----- evaluator: shell constructs -----------------------------------------


def _eval_shell() -> list[Metric]:
    """Three shell constructs that silently answer a question nobody asked.

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
               "control: the three gates below are measured over this set")
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


# ----- hotspots (report only) ----------------------------------------------


def _hotspots() -> list[tuple[str, int, int, int]]:
    """Churn x size. Returns (path, commits, lines, score), worst first."""
    _, log = _run([
        "git", "log", f"--since={CFG.CHURN_DAYS}.days", "--name-only", "--pretty=format:",
    ])
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
    return rows[: CFG.HOTSPOT_COUNT]


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
)


# ----- ratchet -------------------------------------------------------------


def _load_baseline() -> dict[str, float]:
    if not BASELINE.exists():
        return {}
    return json.loads(BASELINE.read_text())["metrics"]


def _compare(
    metrics: list[Metric], baseline: dict[str, float]
) -> tuple[list[str], list[str], list[str]]:
    regressions: list[str] = []
    ratchets: list[str] = []
    unknown: list[str] = []
    for m in metrics:
        if m.key in HARD_ZERO:
            if m.value > 0:
                regressions.append(f"{m.key}: {m.value:g} (hard gate, must be 0)")
            continue
        if m.key in REPORT_ONLY:
            continue
        if m.key not in baseline:
            unknown.append(f"{m.key}: {m.value:g} (no baseline; run --update-baseline)")
        elif m.value > baseline[m.key]:
            regressions.append(f"{m.key}: {m.value:g} > {baseline[m.key]:g} allowed")
        elif m.value < baseline[m.key]:
            ratchets.append(f"{m.key}: {m.value:g} < {baseline[m.key]:g} allowed")
    return regressions, ratchets, unknown


# ----- report --------------------------------------------------------------


def _markdown(metrics: list[Metric], baseline: dict[str, float], hotspots: list) -> str:
    lines = [
        "# Code quality report",
        "",
        f"Generated {datetime.now(UTC).isoformat(timespec='seconds')} by "
        "`python -m scripts.quality_gate`.",
        "",
        "Every number is compared against `ops/quality/baseline.json`. Only a worse",
        "number fails the build; a better number is a ratchet you can bank with",
        "`--update-baseline`.",
        "",
        "| metric | value | allowed | status | unit | worst offender |",
        "| ------ | ----: | ------: | ------ | ---- | -------------- |",
    ]
    for m in metrics:
        if m.key in HARD_ZERO:
            allowed = "0 (hard)"
        elif m.key in REPORT_ONLY:
            allowed = "n/a"
        elif m.key in baseline:
            allowed = f"{baseline[m.key]:g}"
        else:
            allowed = "-"
        if m.key in HARD_ZERO:
            status = "PASS" if m.value == 0 else "FAIL"
        elif m.key in REPORT_ONLY:
            status = "report only"
        elif m.key not in baseline:
            status = "NEW"
        elif m.value > baseline[m.key]:
            status = "REGRESSED"
        elif m.value < baseline[m.key]:
            status = "RATCHET"
        else:
            status = "held"
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
    return "\n".join(lines) + "\n"


# ----- main ----------------------------------------------------------------


def _preflight(selected: list[Evaluator]) -> None:
    if shutil.which("uv") is None:
        raise RuntimeError("uv is not on PATH; see CLAUDE.md (uv, never pip)")
    if any(e.needs_node for e in selected) and shutil.which("pnpm") is None:
        raise RuntimeError("pnpm is not on PATH but a frontend evaluator was selected")


def _marker(metric: Metric, allowed: float | None) -> str:
    """Two-character status flag for one metric line."""
    if metric.key in HARD_ZERO:
        return "!!" if metric.value > 0 else "OK"
    if metric.key in REPORT_ONLY:
        return "--"
    if allowed is None:
        return "??"
    if metric.value > allowed:
        return "!!"
    if metric.value < allowed:
        return "->"
    return "  "


def _print_metrics(metrics: list[Metric], baseline: dict[str, float]) -> None:
    print()
    for m in metrics:
        allowed = baseline.get(m.key)
        if m.key in REPORT_ONLY:
            suffix = " (report only, not gated)"
        elif allowed is not None:
            suffix = f" (allowed {allowed:g})"
        else:
            suffix = ""
        detail = f"  [{m.detail}]" if m.detail else ""
        print(f" {_marker(m, allowed):2s} {m.key:38s} {m.value:>8g} {m.unit}{suffix}{detail}")


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
    if BASELINE.exists():
        existing = json.loads(BASELINE.read_text())
        if "burn_down" in existing:
            payload["burn_down"] = existing["burn_down"]
    # REPORT_ONLY metrics are deliberately absent: this file is a list of
    # allowances, and a number nothing is allowed to exceed does not belong
    # in it.
    payload["metrics"] = {
        m.key: m.value for m in metrics if m.key not in REPORT_ONLY
    }
    BASELINE.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"\n[quality] baseline written to {BASELINE}")


def _select(only: str | None) -> list[Evaluator]:
    wanted = set(only.split(",")) if only else {e.name for e in EVALUATORS}
    unknown_names = wanted - {e.name for e in EVALUATORS}
    if unknown_names:
        raise SystemExit(f"unknown evaluator(s): {', '.join(sorted(unknown_names))}")
    return [e for e in EVALUATORS if e.name in wanted]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--only", help="comma separated evaluator names")
    parser.add_argument("--update-baseline", action="store_true",
                        help="write current values as the new allowance")
    parser.add_argument("--report", type=Path, help="write a markdown report here")
    parser.add_argument("--json", type=Path, help="write raw metrics JSON here")
    parser.add_argument("--list", action="store_true", help="list evaluators and exit")
    args = parser.parse_args(argv)

    if args.list:
        for e in EVALUATORS:
            print(f"{e.name:12s} {e.title}")
        return 0

    selected = _select(args.only)
    _preflight(selected)

    metrics: list[Metric] = []
    for evaluator in selected:
        print(f"[quality] {evaluator.name}: {evaluator.title}", flush=True)
        metrics.extend(evaluator.run())

    baseline = _load_baseline()
    regressions, ratchets, unknown = _compare(metrics, baseline)
    hotspots = _hotspots()

    _print_metrics(metrics, baseline)

    # Absolute paths in the readout: this runs from justfile, Makefile and CI
    # with three different working directories, so a relative path is a path
    # the reader has to reconstruct before they can open it.
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(_markdown(metrics, baseline, hotspots))
        print(f"\n[quality] report written to {args.report.resolve()}")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps({m.key: m.value for m in metrics}, indent=2) + "\n")
        print(f"[quality] metrics written to {args.json.resolve()}")

    if args.update_baseline:
        if len(selected) != len(EVALUATORS):
            raise SystemExit("--update-baseline requires a full run (drop --only)")
        _write_baseline(metrics)
        return 0

    print()
    for line in ratchets:
        print(f"[quality] RATCHET AVAILABLE  {line}")
    for line in unknown:
        print(f"[quality] NO BASELINE       {line}")
    for line in regressions:
        print(f"[quality] REGRESSION        {line}")

    if regressions:
        print(f"\n[quality] FAIL: {len(regressions)} metric(s) got worse.")
        return 1
    print("\n[quality] PASS: nothing got worse.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
