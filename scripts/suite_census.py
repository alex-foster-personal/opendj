"""Test-suite census: per-test coverage redundancy, unmeasured share and time concentration.

A report, never a gate. It answers "which tests could go, and which are slow" with evidence,
for the test-cull workstream (``specs/test-cull/test-cull-spec.md``) and its nightly run on
nucbox (fleet-af ``tenants/open-dj``).

``run`` executes pytest under coverage with one context per test (``--cov-context=test``),
child Python processes credited to the spawning test (``scripts.pytest_child_coverage``), and a
junit file for durations. ``analyze`` then buckets every test case:

- KEEP_COVER: in a minimal set of tests that reproduces the union of covered branch arcs
  (lazy greedy weighted set cover, cheapest arcs-per-second first, then a reverse prune).
- REDUNDANT_IN_SET: removable TOGETHER with that union unchanged. Arc coverage is not
  assertion power, so these are candidates for mutation testing, never automatic deletions.
- NO_COVERAGE: ran no measured Python (config/text readers, non-Python subprocesses): UNKNOWN.

Arcs the parent ran at import time (the unlabelled context) are excluded from every test, so a
child process re-importing a module is not credited with import bodies the parent never credits.

Exit codes: 0 measured; 3 UNKNOWN (the instrument could not measure: no test contexts, a
context naming no junit test, ids that do not join); 1 error (pytest crashed or refused).
A failing test inside the suite is data, not a census failure.

Usage:
    python -m scripts.suite_census run --paths tests/scripts --workers 4 --out-dir .tmp/census
    python -m scripts.suite_census analyze --db .tmp/census/.coverage --junit .tmp/census/junit.xml \
        --cores-dir .tmp/census/cores --out-dir .tmp/census
"""

from __future__ import annotations

import argparse
import heapq
import json
import math
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TypedDict

from scripts.pytest_child_coverage import CORE_DIR_ENV

SOURCES = ("apps", "scripts")
PER_TEST_TIMEOUT_S = 600
PYTEST_MEASURED_EXITS = (0, 1)  # all passed, or some tests failed: both are measurements
EXIT_UNKNOWN = 3
# Cores whose per-test contexts were validated by the control. ``SysMonitor`` (CPython 3.12+)
# dropped a later test's context in round 0, so it is refused until a control passes under it.
VALIDATED_CORES = frozenset({"CTracer", "PyTracer"})

Arc = tuple[int, int, int]  # (file_id, from_line, to_line)


class CaseRow(TypedDict):
    test: str
    bucket: str
    arcs: int
    unique_arcs: int
    dur_s: float


class CensusUnknown(Exception):
    """The instrument could not measure; never render this as a verdict."""


@dataclass
class Census:
    cases: int
    serial_s: float
    union_arcs: int
    import_arcs: int
    child_contexts: int
    buckets: dict[str, dict[str, float]]
    duration_p50_s: float
    duration_p90_s: float
    duration_p99_s: float
    slowest_5pct_share: float
    rows: list[CaseRow] = field(repr=False)


# ----- coverage + junit loading


def _test_id(context: str) -> str | None:
    """`a.py::t|run` and `a.py::t|subprocess` both belong to `a.py::t`; '' is import time."""
    return context.rsplit("|", 1)[0] if context else None


def _load_arcs(db: Path) -> tuple[dict[str, set[Arc]], set[Arc], int]:
    con = sqlite3.connect(db)
    contexts = dict(con.execute("select id, context from context"))
    by_test: dict[str, set[Arc]] = defaultdict(set)
    import_arcs: set[Arc] = set()
    for file_id, context_id, from_line, to_line in con.execute("select file_id, context_id, fromno, tono from arc"):
        tid = _test_id(contexts[context_id])
        if tid:
            by_test[tid].add((file_id, from_line, to_line))
        elif tid is None:
            import_arcs.add((file_id, from_line, to_line))
    child_contexts = sum(1 for c in contexts.values() if c.endswith("|subprocess"))
    if not by_test:
        raise CensusUnknown(f"{db}: no per-test contexts recorded ({len(contexts)} contexts total)")
    return by_test, import_arcs, child_contexts


def _junit_test_id(case: ET.Element) -> str:
    """Rebuild the pytest nodeid, class path included (xunit1 ``file`` + dotted ``classname``)."""
    path, classname, name = case.get("file"), case.get("classname", ""), case.get("name")
    if not path or not name:
        raise CensusUnknown(f"junit testcase without file or name: {case.attrib}")
    module = path.removesuffix(".py").replace("/", ".")
    if classname == module:
        return f"{path}::{name}"
    if classname.startswith(module + "."):
        return f"{path}::{classname[len(module) + 1 :].replace('.', '::')}::{name}"
    raise CensusUnknown(f"junit classname {classname!r} does not start with module {module!r}")


def _junit_duration(case: ET.Element, test_id: str) -> float:
    """The case's measured seconds; a missing, non-finite or negative time is UNKNOWN, never 0."""
    raw = case.get("time")
    if raw is None:
        raise CensusUnknown(f"junit testcase {test_id!r} has no time attribute")
    try:
        seconds = float(raw)
    except ValueError as exc:
        raise CensusUnknown(f"junit testcase {test_id!r} has unparseable time {raw!r}") from exc
    if not math.isfinite(seconds) or seconds < 0:
        raise CensusUnknown(f"junit testcase {test_id!r} has invalid time {raw!r}")
    return seconds


def _load_durations(junit: Path) -> dict[str, float]:
    durations: dict[str, float] = {}
    for case in ET.parse(junit).iter("testcase"):
        test_id = _junit_test_id(case)
        if test_id in durations:
            raise CensusUnknown(f"two junit testcases rebuild to the same id {test_id!r}")
        durations[test_id] = _junit_duration(case, test_id)
    if not durations:
        raise CensusUnknown(f"{junit}: no testcases")
    return durations


# ----- set cover


def dominated_tests(arcs_by_test: dict[str, set[Arc]], durations: dict[str, float]) -> set[str]:
    """Tests whose arcs another test also covers: a strict superset, or an identical set held by a
    cheaper test (ties broken by id). Every dominated test's arcs stay covered by one that is not."""
    holders: dict[Arc, set[str]] = defaultdict(set)
    for t, arcs in arcs_by_test.items():
        for arc in arcs:
            holders[arc].add(t)
    dominated: set[str] = set()
    for t, arcs in arcs_by_test.items():
        rarest = min(arcs, key=lambda arc: len(holders[arc]))
        for u in holders[rarest] - {t}:
            if not arcs <= arcs_by_test[u]:
                continue
            if len(arcs) < len(arcs_by_test[u]) or (durations.get(u, 0.0), u) < (durations.get(t, 0.0), t):
                dominated.add(t)
                break
    return dominated


def minimal_cover(arcs_by_test: dict[str, set[Arc]], durations: dict[str, float]) -> set[str]:
    """Tests whose arcs reproduce the union; asserts the result before returning it.

    Dominated tests are removed before selection, so a strict subset is never kept (Sol P2, #4777),
    then a lazy greedy cover runs and a reverse prune drops anything the rest already holds.
    """
    union: set[Arc] = set().union(*arcs_by_test.values())
    dominated = dominated_tests(arcs_by_test, durations)
    arcs_by_test = {t: arcs for t, arcs in arcs_by_test.items() if t not in dominated}
    remaining = set(union)

    def _score(t: str) -> float:
        return len(arcs_by_test[t] & remaining) / (durations.get(t, 0.0) + 0.01)

    heap = [(-_score(t), t) for t, arcs in arcs_by_test.items() if arcs]
    heapq.heapify(heap)
    keep: list[str] = []
    while remaining and heap:  # lazy greedy: a score only falls as `remaining` shrinks
        _, t = heapq.heappop(heap)
        score = _score(t)
        if score == 0:
            continue
        if heap and score < -heap[0][0]:
            heapq.heappush(heap, (-score, t))
            continue
        keep.append(t)
        remaining -= arcs_by_test[t]

    # reverse prune: greedy can keep a cheap subset test and then still need its superset
    holders: dict[Arc, int] = defaultdict(int)
    for t in keep:
        for arc in arcs_by_test[t]:
            holders[arc] += 1
    for t in sorted(keep, key=lambda t: (-durations.get(t, 0.0), len(arcs_by_test[t]))):
        if all(holders[arc] > 1 for arc in arcs_by_test[t]):
            keep.remove(t)
            for arc in arcs_by_test[t]:
                holders[arc] -= 1

    kept = set(keep)
    reproduced = set().union(*(arcs_by_test[t] for t in kept)) if kept else set()
    if reproduced != union:
        raise AssertionError(f"cover lost {len(union - reproduced)} arcs")
    return kept


# ----- analysis


def _percentile(sorted_values: list[float], q: float) -> float:
    return sorted_values[min(len(sorted_values) - 1, int(len(sorted_values) * q))]


def analyze(db: Path, junit: Path) -> Census:
    arcs_by_test, import_arcs, child_contexts = _load_arcs(db)
    durations = _load_durations(junit)
    orphans = sorted(set(arcs_by_test) - set(durations))
    if orphans:
        raise CensusUnknown(f"{len(orphans)} coverage contexts name no junit test, e.g. {orphans[:3]}")
    for t in arcs_by_test:
        arcs_by_test[t] -= import_arcs
    measured = {t: arcs for t, arcs in arcs_by_test.items() if arcs}
    kept = minimal_cover(measured, durations) if measured else set()

    owners: dict[Arc, int] = defaultdict(int)
    for arcs in measured.values():
        for arc in arcs:
            owners[arc] += 1
    rows: list[CaseRow] = []
    for t in sorted(durations):
        arcs = measured.get(t, set())
        if not arcs:
            bucket = "NO_COVERAGE"
        elif t in kept:
            bucket = "KEEP_COVER"
        else:
            bucket = "REDUNDANT_IN_SET"
        rows.append(
            {
                "test": t,
                "bucket": bucket,
                "arcs": len(arcs),
                "unique_arcs": sum(1 for a in arcs if owners[a] == 1),
                "dur_s": round(durations[t], 3),
            }
        )

    buckets: dict[str, dict[str, float]] = {}
    for name in ("KEEP_COVER", "REDUNDANT_IN_SET", "NO_COVERAGE"):
        members = [r for r in rows if r["bucket"] == name]
        buckets[name] = {"cases": len(members), "serial_s": round(sum(r["dur_s"] for r in members), 1)}
    times = sorted(r["dur_s"] for r in rows)
    serial = sum(times)
    slowest = times[int(len(times) * 0.95) :]
    return Census(
        cases=len(rows),
        serial_s=round(serial, 1),
        union_arcs=len(owners),
        import_arcs=len(import_arcs),
        child_contexts=child_contexts,
        buckets=buckets,
        duration_p50_s=_percentile(times, 0.5),
        duration_p90_s=_percentile(times, 0.9),
        duration_p99_s=_percentile(times, 0.99),
        slowest_5pct_share=round(sum(slowest) / serial, 3) if serial else 0.0,
        rows=rows,
    )


# ----- run


def verify_cores(cores_dir: Path) -> list[str]:
    """Every test process's recorded coverage core, refused unless all are validated (Sol P1, #4777)."""
    cores = sorted(f.read_text(encoding="utf-8").strip() for f in cores_dir.glob("*.core"))
    if not cores:
        raise CensusUnknown(f"no coverage core recorded under {cores_dir}; the core that ran is unproven")
    unvalidated = sorted(set(cores) - VALIDATED_CORES)
    if unvalidated:
        raise CensusUnknown(f"coverage core {unvalidated} is not validated for per-test contexts")
    return cores


VERDICT_FILES = ("census.json", "rows.json")


def clear_previous_outputs(out_dir: Path) -> None:
    """Remove an earlier run's verdict and coverage fragments so a failed run cannot pass for it.

    A reused out-dir kept a previous census.json beside a failed run's log (Sol P2, #4777), and a
    crashed run's ``.coverage.*`` fragments would be combined into the next run's data.
    """
    for name in VERDICT_FILES:
        (out_dir / name).unlink(missing_ok=True)
    for fragment in out_dir.glob(".coverage.*"):
        fragment.unlink()


def _write_coveragerc(out_dir: Path) -> Path:
    rc = out_dir / "coveragerc"
    rc.write_text(
        "[run]\nbranch = true\ncore = ctrace\nrelative_files = true\nparallel = true\npatch = subprocess\n"
        f"source = {', '.join(SOURCES)}\nomit = */tests/*\ndata_file = {out_dir.resolve() / '.coverage'}\n",
        encoding="utf-8",
    )
    return rc


def run_suite(paths: list[str], workers: int, out_dir: Path) -> tuple[int, float]:
    out_dir.mkdir(parents=True, exist_ok=True)
    clear_previous_outputs(out_dir)
    (out_dir / ".coverage").unlink(missing_ok=True)
    rc = _write_coveragerc(out_dir)
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        *paths,
        "-n",
        str(workers),
        "--dist",
        "loadgroup",
        "-p",
        "no:cacheprovider",
        "-p",
        "scripts.pytest_child_coverage",
        "--cov",
        f"--cov-config={rc}",
        "--cov-context=test",
        "--cov-report=",
        f"--junitxml={out_dir / 'junit.xml'}",
        "-o",
        "junit_family=xunit1",
        f"--timeout={PER_TEST_TIMEOUT_S}",
        "-q",
    ]
    cores_dir = out_dir / "cores"
    shutil.rmtree(cores_dir, ignore_errors=True)
    env = {**os.environ, CORE_DIR_ENV: str(cores_dir)}
    started = time.monotonic()
    with (out_dir / "pytest.log").open("w", encoding="utf-8") as log:
        exit_code = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, env=env, check=False).returncode
    return exit_code, round(time.monotonic() - started, 1)


def _git_head() -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()


def _write_outputs(census: Census, out_dir: Path, extra: dict) -> None:
    summary = {k: v for k, v in asdict(census).items() if k != "rows"} | extra
    (out_dir / "rows.json").write_text(json.dumps(census.rows), encoding="utf-8")
    (out_dir / "census.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(json.dumps(summary, indent=1))


# ----- CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    run_p = sub.add_parser("run", help="run pytest under per-test coverage, then analyze")
    run_p.add_argument("--paths", nargs="+", required=True)
    run_p.add_argument("--workers", type=int, required=True)
    run_p.add_argument("--out-dir", type=Path, required=True)
    an_p = sub.add_parser("analyze", help="analyze an existing coverage DB and junit file")
    an_p.add_argument("--db", type=Path, required=True)
    an_p.add_argument("--junit", type=Path, required=True)
    an_p.add_argument("--cores-dir", type=Path, required=True, help="the cores/ dir the run recorded")
    an_p.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args(argv)

    extra: dict = {"measured_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if args.cmd == "run":
        exit_code, wall_s = run_suite(args.paths, args.workers, args.out_dir)
        extra |= {
            "sha": _git_head(),
            "paths": args.paths,
            "workers": args.workers,
            "pytest_exit": exit_code,
            "wall_s": wall_s,
        }
        if exit_code not in PYTEST_MEASURED_EXITS:
            print(f"[ERROR] pytest exited {exit_code}; see {args.out_dir / 'pytest.log'}", file=sys.stderr)
            return 1
        db, junit, cores_dir = args.out_dir / ".coverage", args.out_dir / "junit.xml", args.out_dir / "cores"
    elif args.cmd == "analyze":
        db, junit, cores_dir = args.db, args.junit, args.cores_dir
        args.out_dir.mkdir(parents=True, exist_ok=True)
        clear_previous_outputs(args.out_dir)
    else:
        raise AssertionError(f"unhandled command {args.cmd}")
    try:
        extra["cores"] = sorted(set(verify_cores(cores_dir)))
        census = analyze(db, junit)
    except CensusUnknown as exc:
        print(f"[UNKNOWN] {exc}", file=sys.stderr)
        return EXIT_UNKNOWN
    _write_outputs(census, args.out_dir, extra)
    return 0


if __name__ == "__main__":
    sys.exit(main())
