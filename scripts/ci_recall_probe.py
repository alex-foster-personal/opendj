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

Shadow only: nothing in CI reads it. `(private path)` round 24 logs its numbers.

[if] a module whose tests ran a file in the run phase is not selected for a change to that file [then] it prints as a run-phase MISS.
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


@dataclass(frozen=True)
class Miss:
    module: str
    path: str
    phases: frozenset[str]  # pytest-cov's context suffixes: setup, run, teardown

    @property
    def in_run_phase(self) -> bool:
        return not self.phases <= FIXTURE_PHASES


@dataclass(frozen=True)
class Score:
    pairs: int
    scoped: int  # pairs whose one-file change plans SCOPED; FULL selects every module
    misses: tuple[Miss, ...]

    @property
    def run_phase_misses(self) -> tuple[Miss, ...]:
        return tuple(m for m in self.misses if m.in_run_phase)

    def recall(self, misses: int) -> float:
        return (self.scoped - misses) / self.scoped


def sample_modules(durations: Mapping[str, float], size: int, seed: int, root: Path = REPO) -> tuple[str, ...]:
    """`size` test modules, seeded, from those whose tests sum under `MAX_MODULE_SECONDS`."""
    seconds: dict[str, float] = defaultdict(float)
    for test_id, spent in durations.items():
        seconds[test_id.split("::")[0]] += spent
    pool = sorted(
        module
        for module, total in seconds.items()
        if total < MAX_MODULE_SECONDS
        and module.startswith("tests/")
        and not module.startswith(EXCLUDED_PREFIXES)
        and (root / module).is_file()
    )
    return tuple(sorted(random.Random(seed).sample(pool, size)))


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
    scoped = 0
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
        narrowed = narrow(planned, changes, config, (module,), index, guards)
        if narrowed.verdict is Verdict.SCOPED and not matches(module, narrowed.test_paths):
            misses.append(Miss(module, path, ran))
    return Score(len(pairs), scoped, tuple(misses))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--size", type=int, required=True)
    parser.add_argument("--seed", type=int, required=True)
    args = parser.parse_args(argv)
    modules = sample_modules(json.loads((REPO / ".test_durations").read_text()), args.size, args.seed)
    with tempfile.TemporaryDirectory() as scratch:
        data_file = Path(scratch) / ".coverage"
        ran = _run_under_coverage(modules, data_file)
        data = coverage.CoverageData(basename=str(data_file))
        data.read()
        pairs = executed_pairs(data, REPO)
    try:
        config = read_config()
        real = score(pairs, config, read_reach_index(REPO, modules), frozenset(always_run_modules(REPO)))
    except PlanError as exc:
        print(f"UNKNOWN: {exc}", file=sys.stderr)
        return 3
    control = score(pairs, config, {m: Reach(frozenset(), frozenset(), frozenset()) for m in modules}, frozenset())
    print(f"sample={len(modules)} seed={args.seed} pytest_rc={ran} pairs={real.pairs} scoped={real.scoped}")
    if not real.scoped or not control.run_phase_misses:
        print("UNKNOWN: no SCOPED pair, or the empty-reach control finds no run-phase miss", file=sys.stderr)
        return 3
    for label, result in (("real", real), ("control", control)):
        run_phase = len(result.run_phase_misses)
        print(
            f"{label}: misses={len(result.misses)} run_phase={run_phase} fixture_phase_only={len(result.misses) - run_phase}"
            f" scoped_recall={result.recall(len(result.misses)):.4f} run_phase_recall={result.recall(run_phase):.4f}"
        )
    for miss in real.misses:
        print(
            f"MISS {'run' if miss.in_run_phase else 'fixture'} {miss.module} {miss.path} {'+'.join(sorted(miss.phases))}"
        )
    return 0


# ----- helpers -----


def _run_under_coverage(modules: tuple[str, ...], data_file: Path) -> int:
    """pytest's exit code; a failing test still records what it executed, so it is reported, not fatal.
    Its output goes to stderr, so stdout carries only the probe's numbers."""
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-n", "4"]
    command += ["--cov=apps", "--cov-context=test", "--cov-report=", *modules]
    return subprocess.run(
        command, cwd=REPO, env={**os.environ, "COVERAGE_FILE": str(data_file)}, stdout=sys.stderr, check=False
    ).returncode


if __name__ == "__main__":
    sys.exit(main())
