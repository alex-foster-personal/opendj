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
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ----- config --------------------------------------------------------------

REPO = Path(__file__).resolve().parent.parent
FRONTEND = REPO / "apps" / "webui" / "frontend"
TOOL_REQS = REPO / "ops" / "quality" / "requirements.txt"
BASELINE = REPO / "ops" / "quality" / "baseline.json"


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
# architecture rule with a growing allowance is not a rule.
HARD_ZERO: frozenset[str] = frozenset({"arch.contracts_broken"})

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
REPORT_ONLY: frozenset[str] = frozenset({"deps.issues"})


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


def _run(cmd: list[str], cwd: Path = REPO, allow_fail: bool = False) -> tuple[int, str]:
    """Run a command, returning (exit code, stdout). Fails fast by default."""
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode != 0 and not allow_fail:
        raise RuntimeError(
            f"{' '.join(cmd[:4])}... exited {proc.returncode}\n"
            f"stdout: {proc.stdout[-2000:]}\nstderr: {proc.stderr[-2000:]}"
        )
    return proc.returncode, proc.stdout


def _uv(*args: str, allow_fail: bool = False) -> tuple[int, str]:
    """Run a pinned Python tool in a throwaway env, never the project venv."""
    if not TOOL_REQS.exists():
        raise RuntimeError(f"missing pinned tool manifest: {TOOL_REQS}")
    cmd = [
        "uv", "run", "--no-project", "--quiet",
        "--with-requirements", str(TOOL_REQS),
        *args,
    ]
    return _run(cmd, allow_fail=allow_fail)


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


def _frontend_files() -> list[Path]:
    src = FRONTEND / "src"
    return [
        p
        for p in src.rglob("*")
        if p.suffix in {".ts", ".svelte", ".js"} and p.is_file()
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

    _, mi_raw = _uv("radon", "mi", *paths, "-j", allow_fail=True)
    mi = json.loads(mi_raw)
    low_mi = sorted(
        (
            (file, data["mi"])
            for file, data in mi.items()
            if isinstance(data, dict) and "rank" in data
            and data["rank"] != "A" and not _is_vendored(file)
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
    out_path = Path("/tmp/quality-gate-deptry.json")
    out_path.unlink(missing_ok=True)
    # ROOT must be "." not "apps": given "apps" deptry treats the package's own
    # modules as third party and reports 400+ phantom DEP001s for `import apps.x`.
    # Scope and name mapping live in [tool.deptry] in pyproject.toml.
    _uv("deptry", ".", "--json-output", str(out_path), allow_fail=True)
    if not out_path.exists():
        raise RuntimeError("deptry produced no JSON output; the invocation is wrong")
    issues = json.loads(out_path.read_text())
    by_code: collections.Counter[str] = collections.Counter(i["error"]["code"] for i in issues)
    return [
        Metric(
            "deps.issues",
            len(issues),
            "dependency defects",
            ", ".join(f"{c}={n}" for c, n in by_code.most_common()),
        )
    ]


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

    return [
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

    _pnpm_dlx(
        CFG.JSCPD,
        "--reporters", "json", "--output", "/tmp/jscpd-quality-gate", "--silent",
        "--min-lines", str(CFG.DUP_MIN_LINES), "--min-tokens", str(CFG.DUP_MIN_TOKENS),
        str(REPO / "apps"),
        allow_fail=True,
    )
    report = json.loads((Path("/tmp/jscpd-quality-gate") / "jscpd-report.json").read_text())
    percent = round(float(report["statistics"]["total"]["percentage"]), 2)
    clones = int(report["statistics"]["total"]["clones"])

    return [
        Metric("file_size.max_python", py_max, "lines", py_worst),
        Metric("file_size.over_limit_python", py_over, f"files > {CFG.PY_FILE_LIMIT} lines"),
        Metric("file_size.max_frontend", fe_max, "lines", fe_worst),
        Metric("file_size.over_limit_frontend", fe_over, f"files > {CFG.FE_FILE_LIMIT} lines"),
        Metric("duplication.percent", percent, "% duplicated lines", f"{clones} clones"),
    ]


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
    Evaluator("frontend", "Frontend coupling and dead code", _eval_frontend, needs_node=True),
    Evaluator("size", "File bloat and duplication", _eval_size, needs_node=True),
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
