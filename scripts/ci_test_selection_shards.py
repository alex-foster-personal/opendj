"""Shard assignment for affected PR test selection."""

from __future__ import annotations

import json
import math
from pathlib import Path

from scripts.ci_test_budget import unpriced_module_seconds


def module_durations(ledger: dict[str, float]) -> dict[str, float]:
    """Recorded seconds per test module, summed over its node ids."""
    totals: dict[str, float] = {}
    for node, seconds in ledger.items():
        module = node.split("::", 1)[0]
        totals[module] = totals.get(module, 0.0) + float(seconds)
    return totals


#: CI-DYNSHARD: recorded test seconds one shard is planned to carry. Measured Wed 4 Nov 2026:
#: a FULL fifth is ~300 s of ledger time (1539 s union, run 34454681711), and a shard job's
#: fixed setup (checkout, venv, preflight) is 56-151 s (#8438). 240 s keeps an AFFECTED shard
#: under a FULL one while a typical later edit (a handful of modules) needs one runner.
SHARD_TARGET_SECONDS = 240.0


def _module_weights(modules: list[str], durations: dict[str, float]) -> dict[str, float]:
    """Recorded seconds per module; an untimed module costs what the budget prices it at (CI-UNPRICED).

    An empty ledger weighs every module one second, so a fresh checkout still shards fail-open.
    """
    default = unpriced_module_seconds(durations) if durations else 1.0
    return {m: durations.get(m, default) for m in modules}


def planned_shard_count(
    modules: list[str], durations: dict[str, float], max_shards: int, target_s: float = SHARD_TARGET_SECONDS
) -> int:
    """How many shards an AFFECTED selection needs: 0 for none, else ceil(seconds / target) in 1..max."""
    if not modules:
        return 0
    seconds = sum(_module_weights(modules, durations).values())
    return max(1, min(max_shards, math.ceil(seconds / target_s)))


def shard_matrix(record_path: Path, max_shards: int) -> list[int]:
    """The shard numbers to launch, read from the scope job's selection record.

    Fail-open: a missing, unreadable or FULL record, or one without a plan, launches every shard,
    so a crash can only cost runner minutes, never coverage.
    """
    every = list(range(1, max_shards + 1))
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return every
    count = record.get("shard_count") if isinstance(record, dict) else None
    if record.get("mode") != "affected" or not isinstance(count, int) or not 0 <= count <= max_shards:
        return every
    return list(range(1, count + 1))


def assign_shards(modules: list[str], durations: dict[str, float], shards: int) -> list[list[str]]:
    """Greedy least-loaded assignment, heaviest module first; ties go to the lower shard.

    A module the ledger has never timed (a new test file) weighs the ledger's p90 module, the
    price the budget gives it, so the shards it lands on are planned as loaded as they run.
    """
    weights = _module_weights(modules, durations)
    loads = [0.0] * shards
    assignment: list[list[str]] = [[] for _ in range(shards)]
    for module in sorted(modules, key=lambda m: (-weights[m], m)):
        target = min(range(shards), key=lambda i: (loads[i], i))
        assignment[target].append(module)
        loads[target] += weights[module]
    return [sorted(group) for group in assignment]
