"""Round 24: the planner's recall at run time, read off per-test coverage contexts.

Round 23 found recall gaps by reading. This probe measures them: it runs a seeded sample of test
modules under pytest-cov (`--cov=apps --cov-context=test`) and, for each pair of a test module
and an `apps/` file its tests executed, asks whether `plan` plus `narrow` select that module for
a one-file change to that file.

- A pair that plans FULL is selected by construction, so recall is scored over SCOPED pairs.
- A miss whose file ran only in setup or teardown is a global fixture spilling into a module
  that never loads the code (`tests/conftest.py` reopening `ingest_cli_procs`; the modules that
  load it run the same teardown). It is printed beside run-phase recall, never folded into it.
- The same pairs are scored against an empty reach index with no guards, the planted-miss
  control. When that finds no run-phase miss, the sample cannot show one, and the probe reports
  UNKNOWN (exit 3) instead of a recall.
- Round 26: coverage gives a background thread's lines to whichever test is running, so a module
  can run a file in the run phase only because an earlier module in its worker started the thread.
  Each module with a run-phase miss reruns alone; a miss counts only if it reproduces there, and a
  spilled one prints the sample's other modules that ran its file, the thread's starter among them.
  One control-missed module reruns beside them, and UNKNOWN follows if none of its misses reproduce,
  or if any rerun stopped outside its tests or ran no `apps/` file in the run phase.
- Round 27, `--collection`: what importing each test module runs, which no test context records.
  Each sampled module is collected alone (`--collect-only`, fresh process); lines beyond a
  collection that loads only the root conftest are its pairs, phase `collect`, scored with the run
  phase because a broken import errors the module. The sample is drawn from every test module.

Shadow only: nothing in CI reads it. `(private path)` round 24 logs its numbers.

[if] a module whose tests ran a file in the run phase is not selected for a change to that file [then] it prints as a run-phase MISS.
[if] that module run alone no longer runs the file outside setup and teardown [then] it prints as SPILL, not as a reproduced miss.
[if] a module whose import runs a file beyond the root conftest is not selected for a change to that file [then] `--collection` prints it as a MISS.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import tempfile
from collections import defaultdict
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import coverage

from scripts.ci_plan import REPO, Change, Config, Verdict, matches, plan, read_config
from scripts.ci_plan_narrow import Reach, narrow, read_reach_index
from scripts.ci_plan_sources import PlanError
from scripts.ci_test_selection_rules import FullSuite, always_run_modules

MAX_MODULE_SECONDS = 3.0  # a sample module's summed `.test_durations`: the probe stays a few minutes
EXCLUDED_PREFIXES = ("tests/_ci_dur_pad/", "tests/scripts/")  # duration padding; the planner's own tests
FIXTURE_PHASES = frozenset({"setup", "teardown"})
PYTEST_RAN_THE_TESTS = frozenset({0, 1})  # all passed, some failed; 2 to 5 stopped outside the tests
COLLECT = frozenset({"collect"})  # a pair the module's import ran: not a fixture phase
COLLECTED = frozenset({0, 5})  # --collect-only: tests found, or none (the module still imported)
ROOT_CONFTEST = "tests/conftest.py"  # collected as a target, it loads the conftest and finds no test


@dataclass(frozen=True)
class Miss:
    module: str
    path: str
    phases: frozenset[str]  # pytest-cov's context suffixes: setup, run, teardown

    @property
    def in_run_phase(self) -> bool:
        return _ran_in_run_phase(self.phases)


@dataclass(frozen=True)
class Rerun:
    returncode: int  # pytest's exit code for one module run alone
    pairs: Mapping[tuple[str, str], frozenset[str]]


@dataclass(frozen=True)
class Score:
    pairs: int
    scoped: int  # pairs whose one-file change plans SCOPED; FULL selects every module
    run_scoped: int  # of those, pairs whose file ran outside setup and teardown
    misses: tuple[Miss, ...]

    @property
    def run_phase_misses(self) -> tuple[Miss, ...]:
        return tuple(m for m in self.misses if m.in_run_phase)

    @property
    def scoped_recall(self) -> float:
        return (self.scoped - len(self.misses)) / self.scoped

    @property
    def run_phase_recall(self) -> float:
        """Over run-phase pairs only: a fixture-only pair, selected or not, never moves it (Codex on #8155)."""
        return (self.run_scoped - len(self.run_phase_misses)) / self.run_scoped


def sample_modules(
    durations: Mapping[str, float],
    size: int,
    seed: int,
    root: Path,
    band: tuple[float, float] | None = (0.0, MAX_MODULE_SECONDS),
) -> tuple[str, ...]:
    """`size` test modules, seeded, from those whose tests' summed seconds fall in `band` (from, to),
    or with `band=None` from those the ledger has no entry for (round 25: 404 of 1519 in the committed seed)."""
    seconds: dict[str, float] = defaultdict(float)
    for test_id, spent in durations.items():
        seconds[test_id.split("::")[0]] += spent
    candidates = sampleable_modules(root)
    if band is None:
        pool = [module for module in candidates if module not in seconds]
    else:
        pool = [module for module in candidates if module in seconds and band[0] <= seconds[module] < band[1]]
    return tuple(sorted(random.Random(seed).sample(sorted(pool), size)))


def sampleable_modules(root: Path) -> tuple[str, ...]:
    """Every test module under `root`'s tests/, less duration padding and the planner's own tests."""
    on_disk = (path.relative_to(root).as_posix() for path in (root / "tests").rglob("test_*.py"))
    return tuple(sorted(module for module in on_disk if not module.startswith(EXCLUDED_PREFIXES)))


def collection_pairs(
    baseline: Mapping[str, frozenset[int]], collected: Mapping[str, Mapping[str, frozenset[int]]]
) -> dict[tuple[str, str], frozenset[str]]:
    """Each (test module, file) pair whose lines the module's collection ran beyond the baseline, a
    collection that loads only the root conftest. Those load for every module, so they are no one's."""
    return {
        (module, path): COLLECT
        for module, lines in collected.items()
        for path, hit in lines.items()
        if hit - baseline.get(path, frozenset())
    }


def executed_pairs(data: coverage.CoverageData, root: Path) -> dict[tuple[str, str], frozenset[str]]:
    """Each (test module, repository file) pair a test context executed, with the phases it ran in.
    Lines with no test context (collection-time imports) belong to no test and are left out."""
    phases: dict[tuple[str, str], set[str]] = defaultdict(set)
    for measured in data.measured_files():
        path = Path(measured).resolve().relative_to(root.resolve()).as_posix()
        for contexts in (data.contexts_by_lineno(measured) or {}).values():
            for context in contexts:
                if context:
                    test_id, _, phase = context.rpartition("|")
                    phases[(test_id.split("::")[0], path)].add(phase)
    return {pair: frozenset(ran) for pair, ran in phases.items()}


def score(
    pairs: Mapping[tuple[str, str], frozenset[str]],
    config: Config,
    index: Mapping[str, Reach | None],
    guards: frozenset[str],
) -> Score:
    """Whether `plan` plus `narrow` select each pair's module for a one-file change to its file."""
    scoped = run_scoped = 0
    misses: list[Miss] = []
    for (module, path), ran in sorted(pairs.items()):
        changes = (Change("M", path),)
        try:
            planned = plan(changes, config)
        except FullSuite:
            continue
        if planned.verdict is Verdict.FULL:
            continue
        scoped += 1
        run_scoped += _ran_in_run_phase(ran)
        narrowed = narrow(planned, changes, config, (module,), index, guards)
        if narrowed.verdict is Verdict.SCOPED and not matches(module, narrowed.test_paths):
            misses.append(Miss(module, path, ran))
    return Score(len(pairs), scoped, run_scoped, tuple(misses))


def split_by_rerun_alone(
    misses: tuple[Miss, ...], alone: Mapping[tuple[str, str], frozenset[str]]
) -> tuple[tuple[Miss, ...], tuple[Miss, ...]]:
    """(reproduced, spilled): `alone` holds the pairs each missed module executed when run on its own,
    and a miss reproduces only if its module still runs the file outside setup and teardown there."""
    reproduced = tuple(m for m in misses if _ran_in_run_phase(alone.get((m.module, m.path), frozenset())))
    return reproduced, tuple(m for m in misses if m not in reproduced)


def unusable_reruns(reruns: Mapping[str, Rerun]) -> tuple[str, ...]:
    """Modules whose alone run cannot show what they execute: pytest stopped outside the tests (collection
    error, interrupt, nothing collected), or none of its tests ran an `apps/` file outside setup and
    teardown. Their missing coverage is no evidence of spill (Codex on #8182)."""
    return tuple(
        sorted(
            module
            for module, rerun in reruns.items()
            if rerun.returncode not in PYTEST_RAN_THE_TESTS
            or not any(owner == module and _ran_in_run_phase(ran) for (owner, _), ran in rerun.pairs.items())
        )
    )


def other_modules_that_ran(miss: Miss, pairs: Mapping[tuple[str, str], frozenset[str]]) -> tuple[str, ...]:
    """The sample's other modules that executed the missed file in any phase. A spilled thread's starter
    is one of them; xdist does not record which worker ran which module, so the probe cannot say which."""
    return tuple(sorted(module for module, path in pairs if path == miss.path and module != miss.module))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--size", type=int, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--min-seconds", type=float, default=0.0, help="a module's summed `.test_durations`")
    parser.add_argument("--max-seconds", type=float, default=MAX_MODULE_SECONDS)
    parser.add_argument("--untimed", action="store_true", help="draw modules `.test_durations` has no entry for")
    parser.add_argument("--collection", action="store_true", help="score what importing each module runs")
    args = parser.parse_args(argv)
    if args.collection:
        modules = tuple(sorted(random.Random(args.seed).sample(sampleable_modules(REPO), args.size)))
        collected = _collection_pairs_of(modules)
        if collected is None:
            return 3
        header, pairs = "band=collection pytest_rc=-", collected
    else:
        band = None if args.untimed else (args.min_seconds, args.max_seconds)
        durations = json.loads((REPO / ".test_durations").read_text())
        modules = sample_modules(durations, args.size, args.seed, REPO, band)
        ran, pairs = _pairs_under_coverage(modules, workers=4)
        header = f"band={'untimed' if band is None else f'{band[0]:g}-{band[1]:g}s'} pytest_rc={ran}"
    try:
        config = read_config()
        real = score(pairs, config, read_reach_index(REPO, modules), frozenset(always_run_modules(REPO)))
    except PlanError as exc:
        print(f"UNKNOWN: {exc}", file=sys.stderr)
        return 3
    control = score(pairs, config, {m: Reach(frozenset(), frozenset(), frozenset()) for m in modules}, frozenset())
    print(
        f"{header} sample={len(modules)} seed={args.seed} pairs={real.pairs} scoped={real.scoped}"
        f" run_scoped={real.run_scoped}"
    )
    if not real.run_scoped or not control.run_phase_misses:
        print("UNKNOWN: no run-phase SCOPED pair, or the empty-reach control finds no run-phase miss", file=sys.stderr)
        return 3
    for label, result in (("real", real), ("control", control)):
        run_phase = len(result.run_phase_misses)
        print(
            f"{label}: misses={len(result.misses)} run_phase={run_phase} fixture_phase_only={len(result.misses) - run_phase}"
            f" scoped_recall={result.scoped_recall:.4f} run_phase_recall={result.run_phase_recall:.4f}"
        )
    for miss in real.misses:
        print(
            f"MISS {'run' if miss.in_run_phase else 'fixture'} {miss.module} {miss.path} {'+'.join(sorted(miss.phases))}"
        )
    if args.collection or not real.run_phase_misses:
        return 0  # a collection miss already ran alone
    return _rerun_run_phase_misses_alone(real, control, pairs)


# ----- helpers -----


def _ran_in_run_phase(phases: frozenset[str]) -> bool:
    return not phases <= FIXTURE_PHASES


def _rerun_run_phase_misses_alone(real: Score, control: Score, pairs: Mapping[tuple[str, str], frozenset[str]]) -> int:
    planted = control.run_phase_misses[0].module  # a control miss that must reproduce, or the reruns see nothing
    modules = sorted({miss.module for miss in real.run_phase_misses} | {planted})
    reruns = {module: Rerun(*_pairs_under_coverage((module,), workers=0)) for module in modules}
    if unusable := unusable_reruns(reruns):
        codes = ", ".join(f"{module} (pytest rc={reruns[module].returncode})" for module in unusable)
        print(f"UNKNOWN: run alone, these never ran an apps/ file in a test: {codes}", file=sys.stderr)
        return 3
    alone = {pair: ran for rerun in reruns.values() for pair, ran in rerun.pairs.items()}
    planted_misses = tuple(m for m in control.run_phase_misses if m.module == planted)
    if not split_by_rerun_alone(planted_misses, alone)[0]:
        print(f"UNKNOWN: no control miss of {planted} reproduces when it runs alone", file=sys.stderr)
        return 3
    reproduced, spilled = split_by_rerun_alone(real.run_phase_misses, alone)
    print(
        f"alone: reran={len(modules)} reproduced={len(reproduced)} spilled={len(spilled)}"
        f" run_phase_recall={(real.run_scoped - len(reproduced)) / real.run_scoped:.4f}"
    )
    for miss in spilled:
        print(f"SPILL {miss.module} {miss.path} other_modules_that_ran={','.join(other_modules_that_ran(miss, pairs))}")
    return 0


def _collection_pairs_of(modules: tuple[str, ...]) -> dict[tuple[str, str], frozenset[str]] | None:
    """None, after saying why on stderr, when the baseline did not load the conftest. A module whose
    collection stopped (exit 2: an import failed) is left out and named, since its lines are partial."""
    with tempfile.TemporaryDirectory() as scratch:
        base_rc, baseline = _lines_collected(ROOT_CONFTEST, Path(scratch))
        with ThreadPoolExecutor(4) as pool:
            runs = dict(
                zip(modules, pool.map(lambda module: _lines_collected(module, Path(scratch)), modules), strict=True)
            )
    if base_rc != 5 or not baseline:
        print(
            f"UNKNOWN: the baseline collection of {ROOT_CONFTEST} gave rc={base_rc}, {len(baseline)} files",
            file=sys.stderr,
        )
        return None
    for module, (rc, _) in runs.items():
        if rc not in COLLECTED:
            print(f"UNCOLLECTED {module} pytest_rc={rc}")
    return collection_pairs(baseline, {module: lines for module, (rc, lines) in runs.items() if rc in COLLECTED})


def _lines_collected(target: str, scratch: Path) -> tuple[int, dict[str, frozenset[int]]]:
    """pytest's exit code and the lines `--collect-only` ran per repository file, in a fresh process."""
    data_file = scratch / f".coverage.{target.replace('/', '_')}"
    command = [sys.executable, "-m", "pytest", "-q", "--collect-only", "-p", "no:cacheprovider", "-p", "no:xdist"]
    command += ["--cov=apps", "--cov-report=", target]
    env = {**os.environ, "COVERAGE_FILE": str(data_file)}
    rc = subprocess.run(
        command, cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False
    ).returncode
    data = coverage.CoverageData(basename=str(data_file))
    data.read()
    root = REPO.resolve()
    return rc, {
        Path(measured).resolve().relative_to(root).as_posix(): frozenset(data.lines(measured) or ())
        for measured in data.measured_files()
    }


def _pairs_under_coverage(modules: tuple[str, ...], workers: int) -> tuple[int, dict[tuple[str, str], frozenset[str]]]:
    with tempfile.TemporaryDirectory() as scratch:
        data_file = Path(scratch) / ".coverage"
        ran = _run_under_coverage(modules, data_file, workers)
        data = coverage.CoverageData(basename=str(data_file))
        data.read()
        return ran, executed_pairs(data, REPO)


def _run_under_coverage(modules: tuple[str, ...], data_file: Path, workers: int) -> int:
    """pytest's exit code; a failing test still records what it executed, so it is reported, not fatal.
    Its output goes to stderr, so stdout carries only the probe's numbers."""
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-n", str(workers)]
    command += ["--cov=apps", "--cov-context=test", "--cov-report=", *modules]
    return subprocess.run(
        command, cwd=REPO, env={**os.environ, "COVERAGE_FILE": str(data_file)}, stdout=sys.stderr, check=False
    ).returncode


if __name__ == "__main__":
    sys.exit(main())
