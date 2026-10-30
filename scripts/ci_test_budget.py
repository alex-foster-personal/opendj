"""A runner-minute budget per pytest run: price a selection, trim weak links to fit (TEST-BUDGET).

Read by scripts/ci_test_selection.py when `select` is given `--budget ci/test-budget.yml`. The
config names the budget per context (a PR head, a queue draft), the cost model that prices a
selection from the durations ledger, and the weak-link rules a budget may drop, in order. The
math is here; the selector only calls `fit`.

    planned minutes = shards x overhead_min_per_shard + ledger minutes x min_per_ledger_minute

Requirements ((private path)):
  ✔︎ ✅ 🎯 A selection under budget is returned unchanged.
      [if] planned <= budget and any module is dropped [then ⛔️]
  ✔︎ ✅ 🎯 Over budget, weak-link modules are dropped heaviest first, in drop_order, until it fits.
      [if] a lighter weak module is dropped while a heavier one of the same rule stays [then ⛔️]
  ✔︎ ✅ 🎯 A module kept by any strong rule is never dropped, even over budget.
      [if] an `import graph` module is dropped [then ⛔️]
  ✔︎ ✅ 🎯 Strong links alone over budget: nothing more is dropped and over_budget is True.
      [if] over_budget is False while planned > budget [then ⛔️]
  ✔︎ ✅ 🎯 An unreadable config is a hard error, never a silent "no budget".
      [if] a missing or malformed file yields a BudgetConfig [then ⛔️]

Usage: python -m scripts.ci_test_budget show [--config ci/test-budget.yml]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from scripts.affected_tests import REPO

CONFIG_NAME = "ci/test-budget.yml"
CONTEXTS = ("pr_head", "queue_draft")


class BudgetError(Exception):
    """The budget config cannot be read or does not say what it must."""


@dataclass(frozen=True)
class BudgetConfig:
    minutes: dict[str, float]  # context -> runner minutes
    overhead_min_per_shard: float
    min_per_ledger_minute: float
    shards: int
    drop_order: tuple[str, ...]

    def for_context(self, queue_draft: bool) -> float:
        return self.minutes["queue_draft" if queue_draft else "pr_head"]


@dataclass
class Fit:
    budget_min: float
    planned_before_min: float
    planned_min: float
    shards: int
    kept: list[str]
    dropped: list[str]
    dropped_ledger_s: float
    over_budget: bool
    #: Why each dropped module was droppable: the weak rule it was dropped under.
    dropped_under: dict[str, str] = field(default_factory=dict)

    def as_record(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def load_config(path: Path) -> BudgetConfig:
    # Deferred on purpose: ci_test_selection (and the scope-job helpers that import it) run under
    # bare system python3 with no PyYAML, so only the budget-reading path may need it.
    import yaml

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as missing:
        raise BudgetError(f"budget config `{path}` is missing") from missing
    except yaml.YAMLError as bad:
        raise BudgetError(f"budget config `{path}` is not valid YAML: {bad}") from bad
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise BudgetError(f"budget config `{path}` must be a mapping with `version: 1`")
    try:
        minutes = {c: float(raw["runner_minutes"][c]) for c in CONTEXTS}
        model = raw["model"]
        config = BudgetConfig(
            minutes=minutes,
            overhead_min_per_shard=float(model["overhead_min_per_shard"]),
            min_per_ledger_minute=float(model["min_per_ledger_minute"]),
            shards=int(model["shards"]),
            drop_order=tuple(str(rule) for rule in raw["drop_order"]),
        )
    except (KeyError, TypeError, ValueError) as bad:
        raise BudgetError(f"budget config `{path}` is missing or mistypes a field: {bad!r}") from bad
    if any(m <= 0 for m in config.minutes.values()) or config.shards < 1 or not config.drop_order:
        raise BudgetError(f"budget config `{path}`: minutes and shards must be positive, drop_order non-empty")
    return config


# ---------------------------------------------------------------------------
# pricing and fitting
# ---------------------------------------------------------------------------


def planned_minutes(ledger_seconds: float, config: BudgetConfig) -> float:
    return config.shards * config.overhead_min_per_shard + (ledger_seconds / 60.0) * config.min_per_ledger_minute


def _weights(modules: list[str], durations: dict[str, float]) -> dict[str, float]:
    """Ledger seconds per module; an untimed module weighs the median, as sharding does."""
    known = [durations[m] for m in modules if m in durations]
    default = statistics.median(known) if known else 1.0
    return {m: durations.get(m, default) for m in modules}


def droppable_under(reasons: list[str], drop_order: tuple[str, ...]) -> str | None:
    """The drop_order rule a module falls under, or None when any reason is strong.

    A module is weak only when EVERY reason that selected it matches a drop_order entry; the
    entry returned is the earliest in drop_order among its reasons, so a module kept by a
    `coupling` and a `named reference` drops with the couplings.
    """
    ranks: list[int] = []
    for reason in reasons:
        rank = next((i for i, rule in enumerate(drop_order) if reason.startswith(rule)), None)
        if rank is None:
            return None
        ranks.append(rank)
    return drop_order[min(ranks)] if ranks else None


def fit(
    modules: list[str],
    reasons: dict[str, list[str]],
    durations: dict[str, float],
    budget_min: float,
    config: BudgetConfig,
) -> Fit:
    weights = _weights(modules, durations)
    total = sum(weights.values())
    before = planned_minutes(total, config)
    kept, dropped, dropped_under = list(modules), [], {}
    planned = before
    if planned > budget_min:
        for rule in config.drop_order:
            candidates = sorted(
                (m for m in kept if droppable_under(reasons.get(m, []), config.drop_order) == rule),
                key=lambda m: (-weights[m], m),
            )
            for module in candidates:
                if planned <= budget_min:
                    break
                kept.remove(module)
                dropped.append(module)
                dropped_under[module] = rule
                total -= weights[module]
                planned = planned_minutes(total, config)
            if planned <= budget_min:
                break
    return Fit(
        budget_min=budget_min,
        planned_before_min=round(before, 2),
        planned_min=round(planned, 2),
        shards=config.shards,
        kept=sorted(kept),
        dropped=sorted(dropped),
        dropped_ledger_s=round(sum(weights[m] for m in dropped), 1),
        over_budget=planned > budget_min,
        dropped_under=dropped_under,
    )


def summary_line(result: Fit) -> str:
    state = "OVER BUDGET: strong links alone exceed it" if result.over_budget else "within budget"
    return (
        f"TEST BUDGET: {result.budget_min:g} runner-min; planned {result.planned_before_min:g} -> "
        f"{result.planned_min:g} min, dropped {len(result.dropped)} weak-link module(s) "
        f"({result.dropped_ledger_s:g} ledger-s); {state}"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    show = commands.add_parser("show", help="print the budget config as JSON")
    show.add_argument("--config", default=str(REPO / CONFIG_NAME))
    arguments = parser.parse_args(argv)
    try:
        config = load_config(Path(arguments.config))
    except BudgetError as error:
        print(f"[ERROR] {error}", file=sys.stderr)
        return 2
    print(json.dumps(asdict(config), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
