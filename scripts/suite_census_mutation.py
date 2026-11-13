"""Mutation verdict on top of the suite census: which tests catch faults the rest would miss.

A report, never a gate. ``scripts.suite_census`` says which tests are REDUNDANT_IN_SET by branch
arcs; arc coverage is not assertion power, so that bucket is a candidate list only. This module
plants small AST faults (mutants) in the modules those tests reach, under a time budget, re-runs
the reaching tests per mutant, and gives every case one verdict:

- ``keep``: it failed or was skipped in the census run, it is in the census's minimal arc cover
  (KEEP_COVER), or a cost-greedy choice keeping every caught mutant caught by K=2 tests (or by
  all its killers, when it has fewer) needed it in some module.
- ``candidate``: REDUNDANT_IN_SET, passed, every module it reaches was scored to completion with
  it among the scored tests, and no K=2 mutant cover needed it. Candidates are judged against the
  tests that remain, so deleting several at once is the same joint choice; re-score after a batch.
- ``unscored``: anything else (NO_COVERAGE, or a module it reaches has no complete score yet).

Reuses the census's own data: the coverage DB (``.coverage``, read by
``suite_census.load_module_reach``), ``junit.xml`` and ``rows.json`` in the census run dir. No
second coverage index is built. Ported from PR #4801's ``scripts/test_value/{mutate,ledger}.py``
(merged only on a side branch); the CI workflow, codecov config and research dump are not.

SAFETY. A module is rewritten in place and restored in ``finally`` and on SIGTERM/SIGINT. It
refuses a module with uncommitted changes, so a crashed run shows up in ``git diff`` instead of
passing for the original source. Run it only in a dedicated checkout.

Exit codes: 0 measured (a budget stop is a measurement; unscored tests say what is left);
3 UNKNOWN (census inputs missing or inconsistent: no census.json, a coverage DB the census could
not read, HEAD moved since the census); 1 error.

Usage:
    python -m scripts.suite_census_mutation --census-dir .tmp/census --budget-min 30 \\
        [--previous-dir .tmp/census-yesterday/mutation] [--prefix apps/ --prefix scripts/]
"""

from __future__ import annotations

import argparse
import ast
import calendar
import hashlib
import json
import os
import random
import signal
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

import defusedxml.ElementTree as ET

from scripts import suite_census as census
from scripts.pytest_child_coverage import CORE_DIR_ENV, PROCESS_CONFIG_ENV

K = 2
SCHEMA = 1
EXIT_UNKNOWN = census.EXIT_UNKNOWN
CRASH = "<no result: pytest did not finish>"
PYTEST_FINISHED = (0, 1, 2)  # passed, some failed, interrupted by collection errors
DEFAULT_PREFIXES = ("apps/", "scripts/")
# The instrument never mutates itself: a mutant here would change the measurement, not the code
# under test. Tests reaching only these stay ``unscored`` rather than pass as candidates.
NEVER_MUTATE = frozenset(
    {"scripts/suite_census.py", "scripts/suite_census_mutation.py", "scripts/pytest_child_coverage.py"}
)
VERDICT_FILE = "mutation.json"
SCORES_DIR = "mutation"

_SWAP: dict[type, type] = {
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.Lt: ast.GtE,
    ast.GtE: ast.Lt,
    ast.Gt: ast.LtE,
    ast.LtE: ast.Gt,
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
    ast.Add: ast.Sub,
    ast.Sub: ast.Add,
    ast.Mult: ast.FloorDiv,
    ast.And: ast.Or,
    ast.Or: ast.And,
}


# ----- mutants


@dataclass(frozen=True)
class Site:
    kind: str
    lineno: int
    col: int
    node_type: str
    index: int = 0

    @property
    def label(self) -> str:
        return f"{self.kind}@{self.lineno}:{self.col}" + (f".{self.index}" if self.index else "")


def sites(source: str) -> list[Site]:
    """Every mutable site in ``source``, in a stable order.

    Operators: swap a comparison, ``+``/``-``, ``*``/``//``, ``and``/``or``; flip a bool literal;
    bump a 0/1/2 int literal; ``return None``; negate an ``if``.
    """
    found: list[Site] = []
    for node in ast.walk(ast.parse(source)):
        pos = (getattr(node, "lineno", 0), getattr(node, "col_offset", 0), type(node).__name__)
        if isinstance(node, ast.Compare):
            found += [Site("cmp", *pos, i) for i, op in enumerate(node.ops) if type(op) in _SWAP]
        elif isinstance(node, (ast.BinOp, ast.BoolOp)) and type(node.op) in _SWAP:
            found.append(Site("op", *pos))
        elif isinstance(node, ast.Constant) and isinstance(node.value, bool):
            found.append(Site("bool", *pos))
        elif isinstance(node, ast.Constant) and type(node.value) is int and node.value in (0, 1, 2):
            found.append(Site("int", *pos))
        elif isinstance(node, ast.Return) and node.value is not None:
            found.append(Site("ret", *pos))
        elif isinstance(node, ast.If):
            found.append(Site("notif", *pos))
    return sorted(found, key=lambda s: (s.lineno, s.col, s.kind, s.index))


def apply(source: str, site: Site) -> str | None:
    """``source`` with ``site`` mutated, or None when the result is not a real change."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (
            type(node).__name__ != site.node_type
            or getattr(node, "lineno", None) != site.lineno
            or getattr(node, "col_offset", None) != site.col
        ):
            continue
        if site.kind == "cmp" and isinstance(node, ast.Compare):
            node.ops[site.index] = _SWAP[type(node.ops[site.index])]()
        elif site.kind == "op" and isinstance(node, (ast.BinOp, ast.BoolOp)):
            node.op = _SWAP[type(node.op)]()
        elif site.kind == "bool" and isinstance(node, ast.Constant):
            node.value = not node.value
        elif (
            site.kind == "int"
            and isinstance(node, ast.Constant)
            and type(node.value) is int
            and node.value in (0, 1, 2)
        ):
            node.value = node.value + 1
        elif site.kind == "ret" and isinstance(node, ast.Return):
            node.value = ast.Constant(None)
        elif site.kind == "notif" and isinstance(node, ast.If):
            node.test = ast.UnaryOp(ast.Not(), node.test)
        else:
            return None
        out = ast.unparse(tree)
        return None if out == ast.unparse(ast.parse(source)) else out
    return None


def choose(source: str, module: str, cap: int) -> list[Site]:
    """Up to ``cap`` sites, shuffled by a seed fixed per module path, so a re-score of an
    unchanged module plants the same mutants."""
    all_sites = sites(source)
    random.Random(hashlib.sha256(module.encode()).hexdigest()).shuffle(all_sites)
    return all_sites[:cap]


# ----- running the reaching tests


def junit_outcomes(junit: Path) -> dict[str, str]:
    """pytest node id -> passed | failed | skipped, ids rebuilt exactly as the census does."""
    outcomes: dict[str, str] = {}
    for case in ET.parse(junit).iter("testcase"):
        test_id = census._junit_test_id(case)
        tags = {child.tag for child in case}
        if tags & {"failure", "error"}:
            outcomes[test_id] = "failed"
        elif "skipped" in tags:
            outcomes[test_id] = "skipped"
        else:
            outcomes.setdefault(test_id, "passed")
    return outcomes


def _pytest_env() -> dict[str, str]:
    """No census instrumentation leaks into a mutant run, and no user data dir either."""
    drop = {"MDT_DATA_DIR", PROCESS_CONFIG_ENV, CORE_DIR_ENV}
    return {k: v for k, v in os.environ.items() if k not in drop and not k.startswith("COVERAGE_")}


def run_tests(repo: Path, tests: list[str], timeout_s: float) -> tuple[set[str], set[str]] | None:
    """(failed, absent) node ids, or None when pytest produced no complete result.

    ``absent`` is a requested test that pytest never reported: under a mutant that is a
    collection error caused by the fault, so the caller counts it as a kill.
    """
    with tempfile.TemporaryDirectory(prefix="suite-census-mutation-") as tmp:
        args_file, junit = Path(tmp) / "args.txt", Path(tmp) / "junit.xml"
        args_file.write_text("\n".join(tests), encoding="utf-8")
        cmd = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:randomly",
            "--no-coverage-matrix",
            "-o",
            "addopts=",
            "-o",
            "junit_family=xunit1",
            f"--junitxml={junit}",
            f"--timeout={int(timeout_s)}",
            f"@{args_file}",
        ]
        try:
            # A fresh bytecode cache per run: a mutant written in the same second and at the same
            # size as the file it replaced would otherwise run from the other one's stale .pyc.
            env = _pytest_env() | {"PYTHONPYCACHEPREFIX": str(Path(tmp) / "pycache")}
            proc = subprocess.run(cmd, cwd=repo, env=env, capture_output=True, timeout=timeout_s, check=False)
        except subprocess.TimeoutExpired:
            return None
        if proc.returncode not in PYTEST_FINISHED or not junit.is_file():
            return None
        try:
            outcomes = junit_outcomes(junit)
        except (ET.ParseError, census.CensusUnknown):
            return None
    failed = {t for t, o in outcomes.items() if o == "failed"}
    return failed, set(tests) - set(outcomes)


def _git_clean(repo: Path, module: str) -> bool:
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", module], cwd=repo, capture_output=True, check=False
    )
    diff = subprocess.run(["git", "diff", "--quiet", "HEAD", "--", module], cwd=repo, capture_output=True, check=False)
    return tracked.returncode == 0 and diff.returncode == 0


def _utc_stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime())


@dataclass
class ModuleScore:
    module: str
    source_sha256: str
    scored_at: str
    tests: list[str]
    baseline_failed: list[str]
    baseline_s: float = 0.0
    mutants: list[dict] = field(default_factory=list)
    stopped_early: str | None = None


def score_module(
    repo: Path, module: str, tests: list[str], *, cap: int, deadline: float, per_run_timeout_s: float
) -> ModuleScore:
    """Baseline once, then one run per mutant WITHOUT ``-x``: each mutant records every test
    that caught it. A test already failing (or missing) at baseline can kill nothing."""
    path = repo / module
    if not _git_clean(repo, module):
        raise RuntimeError(f"{module} is untracked or has uncommitted changes; refusing to mutate it")
    original = path.read_text(encoding="utf-8")
    score = ModuleScore(
        module=module,
        source_sha256=hashlib.sha256(original.encode()).hexdigest(),
        scored_at=_utc_stamp(),
        tests=sorted(tests),
        baseline_failed=[],
    )
    started = time.monotonic()
    baseline = run_tests(repo, score.tests, per_run_timeout_s)
    score.baseline_s = round(time.monotonic() - started, 2)
    if baseline is None:
        score.stopped_early = "baseline run did not finish"
        return score
    base_bad = baseline[0] | baseline[1]
    score.baseline_failed = sorted(base_bad)

    def _restore(signum: int, _frame: object) -> None:
        path.write_text(original, encoding="utf-8")
        raise SystemExit(f"signal {signum}: restored {module}")

    old = {s: signal.signal(s, _restore) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        for site in choose(original, module, cap):
            if time.monotonic() + score.baseline_s >= deadline:
                score.stopped_early = "budget"
                break
            mutated = apply(original, site)
            if mutated is None:
                continue
            path.write_text(mutated, encoding="utf-8")
            try:
                result = run_tests(repo, score.tests, per_run_timeout_s)
            finally:
                path.write_text(original, encoding="utf-8")
            killed = sorted((result[0] | result[1]) - base_bad) if result is not None else [CRASH]
            score.mutants.append({"id": site.label, "killed_by": killed})
    finally:
        path.write_text(original, encoding="utf-8")
        for s, handler in old.items():
            signal.signal(s, handler)
    return score


# ----- verdicts


def keep_cover(tests: list[str], kills: list[set[str]], seconds: dict[str, float]) -> set[str]:
    """Cheapest-first greedy choice keeping each caught mutant caught by min(K, killers)."""
    need = [min(K, len(k)) for k in kills]
    chosen: set[str] = set()
    while any(need):
        best, best_gain = None, 0.0
        for t in sorted(tests):
            if t in chosen:
                continue
            hits = sum(1 for i, k in enumerate(kills) if need[i] and t in k)
            if hits:
                gain = hits / (seconds.get(t, 0.0) + 0.01)
                if gain > best_gain:
                    best, best_gain = t, gain
        if best is None:
            break
        chosen.add(best)
        for i, k in enumerate(kills):
            if need[i] and best in k:
                need[i] -= 1
    return chosen


@dataclass
class _MutationFacts:
    kills_by: dict[str, int]
    sole_by: dict[str, int]
    needed_in: dict[str, list[str]]
    module_stats: dict[str, dict]


def _mutation_facts(scores: dict[str, dict], seconds: dict[str, float]) -> _MutationFacts:
    """Per-test kills, sole kills and the modules whose K-cover needs it; per-module stats."""
    kills_by: dict[str, int] = defaultdict(int)
    sole_by: dict[str, int] = defaultdict(int)
    needed_in: dict[str, list[str]] = defaultdict(list)
    module_stats: dict[str, dict] = {}
    for module, s in sorted(scores.items()):
        caught = [k for k in (set(m["killed_by"]) - {CRASH} for m in s["mutants"]) if k]
        for k in caught:
            for t in k:
                kills_by[t] += 1
            if len(k) == 1:
                sole_by[next(iter(k))] += 1
        for t in keep_cover(s["tests"], caught, seconds):
            needed_in[t].append(module)
        module_stats[module] = {
            "mutants": len(s["mutants"]),
            "caught": len(caught),
            "crashed": sum(1 for m in s["mutants"] if CRASH in m["killed_by"]),
            "tests": len(s["tests"]),
            "scored_at": s["scored_at"],
            "source_sha256": s["source_sha256"],
            "stopped_early": s.get("stopped_early"),
        }
    return _MutationFacts(kills_by, sole_by, needed_in, module_stats)


def _reasons_to_keep(row: dict, outcome: str, needed: list[str]) -> list[str]:
    reasons = []
    if outcome != "passed":
        reasons.append(f"outcome {outcome}")
    if row["bucket"] == "KEEP_COVER":
        reasons.append(f"in the minimal arc cover ({row['unique_arcs']} unique arcs)")
    if needed:
        reasons.append("needed to keep mutants caught in " + ", ".join(sorted(needed)))
    return reasons


def build_verdicts(
    rows: list[dict],
    outcomes: dict[str, str],
    reach: dict[str, set[str]],
    scores: dict[str, dict],
) -> dict:
    """One verdict per census row; ``reach`` is ``load_module_reach`` restricted to scored scope."""
    seconds = {r["test"]: float(r["dur_s"]) for r in rows}
    facts = _mutation_facts(scores, seconds)
    kills_by, sole_by, needed_in, module_stats = facts.kills_by, facts.sole_by, facts.needed_in, facts.module_stats
    reached: dict[str, list[str]] = defaultdict(list)
    for module, tests in reach.items():
        for t in tests:
            reached[t].append(module)

    verdicts: dict[str, dict] = {}
    for r in sorted(rows, key=lambda r: r["test"]):
        t, bucket = r["test"], r["bucket"]
        outcome = outcomes.get(t, "absent")
        reasons = _reasons_to_keep(r, outcome, needed_in[t])
        unscored = sorted(
            m for m in reached[t] if m not in scores or scores[m].get("stopped_early") or t not in scores[m]["tests"]
        )
        if reasons:
            verdict = "keep"
        elif bucket != "REDUNDANT_IN_SET" or unscored:
            verdict = "unscored"
        else:
            verdict = "candidate"
        verdicts[t] = {
            "verdict": verdict,
            "reasons": reasons,
            "bucket": bucket,
            "outcome": outcome,
            "dur_s": seconds[t],
            "kills": kills_by[t],
            "sole_kills": sole_by[t],
            "modules_reached": len(reached[t]),
            "modules_unscored": len(unscored),
        }
    counts = {v: {"cases": 0, "serial_s": 0.0} for v in ("keep", "candidate", "unscored")}
    for row in verdicts.values():
        counts[row["verdict"]]["cases"] += 1
        counts[row["verdict"]]["serial_s"] = round(counts[row["verdict"]]["serial_s"] + row["dur_s"], 3)
    return {
        "schema": SCHEMA,
        "verdicts": counts,
        "modules_scored": len(module_stats),
        "mutants": sum(m["mutants"] for m in module_stats.values()),
        "mutants_caught": sum(m["caught"] for m in module_stats.values()),
        "modules": module_stats,
        "tests": verdicts,
    }


# ----- inputs, carry-over and the budgeted run


def _load_scores(directory: Path | None) -> dict[str, dict]:
    if directory is None or not directory.is_dir():
        return {}
    out: dict[str, dict] = {}
    for p in sorted(directory.glob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        out[d["module"]] = d
    return out


def _repo_relative(path: str, repo: Path) -> str:
    """The census records relative paths (``relative_files``); an absolute one is made relative."""
    p = Path(path)
    if not p.is_absolute():
        return p.as_posix()
    try:
        return p.resolve().relative_to(repo).as_posix()
    except ValueError:
        return p.as_posix()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def is_fresh(prev: dict | None, source_sha256: str, now: float, fresh_s: float) -> bool:
    """A carried score that needs no re-score: same source, complete, and recent."""
    if not prev or prev.get("source_sha256") != source_sha256 or prev.get("stopped_early"):
        return False
    scored = calendar.timegm(time.strptime(prev["scored_at"][:19], "%Y-%m-%dT%H:%M:%S"))
    return now - scored < fresh_s


def load_census(census_dir: Path, repo: Path) -> tuple[list[dict], dict[str, str], dict[str, set[str]]]:
    """The census run's rows, junit outcomes and per-module reach; UNKNOWN when it did not measure."""
    summary_path = census_dir / "census.json"
    if not summary_path.is_file():
        raise census.CensusUnknown(f"{summary_path} missing: the census did not measure, so there is nothing to score")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=False)
    if summary.get("sha") and head.stdout.strip() != summary["sha"]:
        raise census.CensusUnknown(f"census measured {summary['sha']} but HEAD is {head.stdout.strip() or '?'}")
    rows = json.loads((census_dir / "rows.json").read_text(encoding="utf-8"))
    outcomes = junit_outcomes(census_dir / "junit.xml")
    reach = census.load_module_reach(census_dir / ".coverage")
    missing = sorted({t for tests in reach.values() for t in tests} - {r["test"] for r in rows})
    if missing:
        raise census.CensusUnknown(f"{len(missing)} coverage tests are not census rows, e.g. {missing[:3]}")
    return rows, outcomes, reach


def run(
    repo: Path,
    census_dir: Path,
    *,
    budget_min: float,
    prefixes: tuple[str, ...],
    previous_dir: Path | None,
    cap: int,
    per_run_timeout_s: float,
    rescore_days: float,
) -> tuple[dict, dict[str, dict]]:
    """The verdict document and every score in force (carried or new)."""
    rows, outcomes, reach_all = load_census(census_dir, repo)
    reach: dict[str, set[str]] = defaultdict(set)
    for measured_path, tests in reach_all.items():
        module = _repo_relative(measured_path, repo)
        if module.endswith(".py") and module.startswith(prefixes):
            reach[module] |= tests
    previous = _load_scores(previous_dir)
    now, fresh_s = time.time(), rescore_days * 86400
    seconds = {r["test"]: float(r["dur_s"]) for r in rows}

    scores: dict[str, dict] = {}
    queue: list[tuple[float, str, list[str]]] = []
    for module, tests in reach.items():
        path = repo / module
        if not path.is_file():
            continue
        sha = _sha(path)
        prev = previous.get(module)
        if prev and prev.get("source_sha256") == sha:
            scores[module] = prev  # carried: still describes this source
        if module in NEVER_MUTATE or is_fresh(prev, sha, now, fresh_s):
            continue
        passing = sorted(t for t in tests if outcomes.get(t) == "passed")
        if passing:
            queue.append((sum(seconds.get(t, 0.0) for t in passing), module, passing))
    queue.sort(reverse=True)  # costliest tests first: that is where a cut pays most

    deadline = time.monotonic() + budget_min * 60
    scored_now: list[str] = []
    skipped: dict[str, str] = {}
    for _cost, module, passing in queue:
        if time.monotonic() >= deadline:
            break
        try:
            score = score_module(repo, module, passing, cap=cap, deadline=deadline, per_run_timeout_s=per_run_timeout_s)
        except RuntimeError as exc:
            skipped[module] = str(exc)
            print(f"[mutation] skip {module}: {exc}", file=sys.stderr)
            continue
        scores[module] = asdict(score)
        scored_now.append(module)
        print(
            f"[mutation] {module}: {len(score.mutants)} mutants, "
            f"{sum(1 for m in score.mutants if m['killed_by'])} caught, {len(passing)} tests "
            f"({score.stopped_early or 'complete'})",
            file=sys.stderr,
        )

    result = build_verdicts(rows, outcomes, reach, scores)
    result |= {
        "budget_min": budget_min,
        "prefixes": list(prefixes),
        "eligible_modules": len(queue),
        "scored_this_run": sorted(scored_now),
        "carried_modules": sorted(set(scores) - set(scored_now)),
        "skipped_modules": skipped,
    }
    return result, scores


def write_outputs(result: dict, out_dir: Path, scores: dict[str, dict]) -> None:
    """``mutation.json`` plus every score in force under ``mutation/``, so tomorrow can carry it."""
    scores_dir = out_dir / SCORES_DIR
    scores_dir.mkdir(parents=True, exist_ok=True)
    for module, s in scores.items():
        (scores_dir / (module.replace("/", "__") + ".json")).write_text(
            json.dumps(s, indent=1, sort_keys=True), encoding="utf-8"
        )
    (out_dir / VERDICT_FILE).write_text(json.dumps(result, indent=1, sort_keys=True), encoding="utf-8")


def summary_of(result: dict) -> dict:
    return {k: v for k, v in result.items() if k not in ("tests", "modules")}


# ----- CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.suite_census_mutation", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument("--census-dir", type=Path, required=True, help="a `suite_census run` out-dir")
    parser.add_argument("--budget-min", type=float, required=True, help="wall minutes to spend planting mutants")
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--out-dir", type=Path, default=None, help="default: the census dir")
    parser.add_argument("--previous-dir", type=Path, default=None, help="an earlier run's mutation/ dir to carry")
    parser.add_argument("--prefix", action="append", default=None, help=f"module prefix (default {DEFAULT_PREFIXES})")
    parser.add_argument("--cap", type=int, default=40, help="mutants per module")
    parser.add_argument("--per-run-timeout-s", type=float, default=300)
    parser.add_argument("--rescore-days", type=float, default=7)
    args = parser.parse_args(argv)
    if args.budget_min <= 0 or args.cap < 1:
        parser.error("--budget-min must be positive and --cap at least 1")
    out_dir = args.out_dir or args.census_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / VERDICT_FILE).unlink(missing_ok=True)  # a failed run cannot pass for an earlier one
    repo = args.repo.resolve()
    try:
        result, scores = run(
            repo,
            args.census_dir,
            budget_min=args.budget_min,
            prefixes=tuple(args.prefix or DEFAULT_PREFIXES),
            previous_dir=args.previous_dir,
            cap=args.cap,
            per_run_timeout_s=args.per_run_timeout_s,
            rescore_days=args.rescore_days,
        )
    except (census.CensusUnknown, FileNotFoundError, json.JSONDecodeError, ET.ParseError) as exc:
        print(f"[UNKNOWN] {exc}", file=sys.stderr)
        return EXIT_UNKNOWN
    result["measured_utc"] = _utc_stamp()
    write_outputs(result, out_dir, scores)
    print(json.dumps(summary_of(result), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
