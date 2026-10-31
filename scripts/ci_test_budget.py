"""A runner-minute budget per pytest run: price a selection, trim weak links to fit (TEST-BUDGET).

Read by scripts/ci_test_selection.py when `select` is given `--budget ci/test-budget.yml`. The
config names the budget per context (a PR head, a queue draft), the cost model that prices a
selection from the durations ledger, and the weak-link rules a budget may drop, in order. The
math is here; the selector only calls `fit`.

    planned minutes = shards x overhead_min_per_shard + ledger minutes x min_per_ledger_minute

Two caps (`cap:` in the config). `weak-links` (round 0) drops only the modules weak rules
selected, so strong links may exceed the budget. `hard` (round 5, the maintainer, Fri 30 Oct 2026: "max 10
total runner minutes as a rule and intelligent selection just gets to choose") ranks EVERY
selected module by its distance from the change (scripts/ci_test_selection_rules.py), then by
recorded seconds ascending, and keeps the nearest until the budget is spent; the rest wait for
the nightly full suite and the trunk checkpoint, which reverts what escapes.

Requirements ((private path), ADR-NEW-hard-cap-per-run.md):
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
  ✔︎ ✅ 🎯 Under the hard cap the planned minutes never exceed the budget, whatever the links.
      [if] planned > budget with cap hard [then ⛔️]
  ✔︎ ✅ 🎯 Under the hard cap a nearer module is never dropped while a farther one is kept.
      [if] a distance-1 module is dropped and a distance-3 module kept [then ⛔️]

Usage: python -m scripts.ci_test_budget show [--config ci/test-budget.yml]
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from scripts.affected_tests import REPO

CONFIG_NAME = "ci/test-budget.yml"
CONTEXTS = ("pr_head", "queue_draft")
CAPS = ("weak-links", "hard")
#: A module declared in the PR body (`CI-Tests:`) sits with the changed modules themselves: the
#: author asked for it by name.
DECLARED_DISTANCE = 0


class BudgetError(Exception):
    """The budget config cannot be read or does not say what it must."""


@dataclass(frozen=True)
class BudgetConfig:
    minutes: dict[str, float]  # context -> runner minutes
    overhead_min_per_shard: float
    min_per_ledger_minute: float
    shards: int
    drop_order: tuple[str, ...]
    cap: str  # required: a caller states which cap it means, never inherits one

    @property
    def hard(self) -> bool:
        return self.cap == "hard"

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
    #: Why each dropped module was droppable: the weak rule it was dropped under, or under the
    #: hard cap its rank (`distance 3`).
    cap: str
    dropped_under: dict[str, str] = field(default_factory=dict)

    def as_record(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def _number(value: object) -> float:
    """A YAML number as a float; a bool or a string is a mistyped field, not 1.0 or a parsed number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"expected a number, got {value!r}")
    return float(value)


def _shards(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"shards must be an integer, got {value!r}")
    return value


def load_config(path: Path) -> BudgetConfig:
    # Deferred on purpose: ci_test_selection (and the scope-job helpers that import it) run under
    # bare system python3 with no PyYAML, so only the budget-reading path may need it.
    try:
        import yaml
    except ImportError as unavailable:
        raise BudgetError(
            f"PyYAML is not importable under {sys.executable}; reading `{path}` needs it (a bare system python3 has none)"
        ) from unavailable

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as missing:
        raise BudgetError(f"budget config `{path}` is missing") from missing
    except (OSError, UnicodeDecodeError) as unreadable:
        raise BudgetError(f"budget config `{path}` cannot be read: {unreadable!r}") from unreadable
    except yaml.YAMLError as bad:
        raise BudgetError(f"budget config `{path}` is not valid YAML: {bad}") from bad
    if not isinstance(raw, dict) or raw.get("version") != 2:
        raise BudgetError(f"budget config `{path}` must be a mapping with `version: 2`")
    try:
        minutes = {c: _number(raw["runner_minutes"][c]) for c in CONTEXTS}
        model = raw["model"]
        config = BudgetConfig(
            minutes=minutes,
            overhead_min_per_shard=_number(model["overhead_min_per_shard"]),
            min_per_ledger_minute=_number(model["min_per_ledger_minute"]),
            shards=_shards(model["shards"]),
            drop_order=tuple(str(rule) for rule in raw["drop_order"]),
            cap=str(raw["cap"]),
        )
    except (KeyError, TypeError, ValueError) as bad:
        raise BudgetError(f"budget config `{path}` is missing or mistypes a field: {bad!r}") from bad
    coefficients = (config.overhead_min_per_shard, config.min_per_ledger_minute, *config.minutes.values())
    if not all(math.isfinite(c) and c > 0 for c in coefficients) or config.shards < 1 or not config.drop_order:
        raise BudgetError(
            f"budget config `{path}`: minutes, shards and both model coefficients must be finite and "
            "positive, drop_order non-empty"
        )
    overhead = config.shards * config.overhead_min_per_shard
    too_small = {c: m for c, m in config.minutes.items() if m < overhead}
    if too_small:
        raise BudgetError(
            f"budget config `{path}`: {too_small} runner-min cannot pay the {overhead:g} runner-min of "
            f"fixed shard overhead ({config.shards} x {config.overhead_min_per_shard:g}), so no plan could fit"
        )
    if config.cap not in CAPS:
        raise BudgetError(f"budget config `{path}`: cap must be one of {CAPS}, not {config.cap!r}")
    return config


# ---------------------------------------------------------------------------
# pricing and fitting
# ---------------------------------------------------------------------------


def planned_minutes(ledger_seconds: float, config: BudgetConfig) -> float:
    return config.shards * config.overhead_min_per_shard + (ledger_seconds / 60.0) * config.min_per_ledger_minute


def _weights(modules: list[str], durations: dict[str, float]) -> dict[str, float]:
    """Ledger seconds per module; an untimed module weighs the median of the selected timed ones.

    A selection with no timed module (every test new or renamed) weighs the median of the whole
    ledger instead, and an empty ledger is a BudgetError: pricing from no evidence at all would let
    a large selection pass as cheap.
    """
    known = [durations[m] for m in modules if m in durations] or list(durations.values())
    if modules and not known:
        raise BudgetError("the durations ledger holds no timing at all, so a selection cannot be priced")
    default = statistics.median(known) if known else 0.0
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
    distance: dict[str, int] | None = None,
) -> Fit:
    """Trim `modules` to `budget_min` the way `config.cap` says; `distance` ranks the hard cap."""
    if config.hard:
        return fit_hard(modules, distance or {}, durations, budget_min, config)
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
        cap=config.cap,
    )


def rank_key(module: str, distance: dict[str, int], weights: dict[str, float]) -> tuple[int, float, str]:
    """Nearest to the change first, then the cheapest, then the name: the hard cap's order."""
    return (distance[module], weights[module], module)


def fit_hard(
    modules: list[str],
    distance: dict[str, int],
    durations: dict[str, float],
    budget_min: float,
    config: BudgetConfig,
) -> Fit:
    """Keep the nearest modules until the budget is spent; no module is exempt.

    The shard overhead is paid whatever is kept, so a budget below it keeps nothing and
    `over_budget` says so; `load_config` rejects such a budget, so a loaded config never gets there.
    """
    unranked = sorted(m for m in modules if m not in distance)
    if unranked:
        raise BudgetError(
            f"selected module(s) carry no distance from the change, so the hard cap cannot rank them: {unranked}"
        )
    weights = _weights(modules, durations)
    before = planned_minutes(sum(weights.values()), config)
    kept, dropped, dropped_under = [], [], {}
    total = 0.0
    ranked = sorted(modules, key=lambda m: rank_key(m, distance, weights))
    for index, module in enumerate(ranked):
        if planned_minutes(total + weights[module], config) > budget_min:
            # Strictly in rank order: the first module that does not fit ends the run, so a
            # farther module is never kept in place of a nearer one it was cheaper than.
            for far in ranked[index:]:
                dropped.append(far)
                dropped_under[far] = f"hard cap, distance {distance[far]}"
            break
        kept.append(module)
        total += weights[module]
    planned = planned_minutes(total, config)
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
        cap=config.cap,
    )


def summary_line(result: Fit) -> str:
    if result.cap == "hard":
        farthest = max((int(why.rsplit(" ", 1)[1]) for why in result.dropped_under.values()), default=None)
        nearest = min((int(why.rsplit(" ", 1)[1]) for why in result.dropped_under.values()), default=None)
        cut = f"nearest dropped at distance {nearest}, farthest {farthest}" if result.dropped else "nothing dropped"
        return (
            f"TEST BUDGET (hard cap): {result.budget_min:g} runner-min; planned {result.planned_before_min:g} -> "
            f"{result.planned_min:g} min, kept {len(result.kept)} nearest module(s), dropped "
            f"{len(result.dropped)} ({result.dropped_ledger_s:g} ledger-s); {cut}"
        )
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
