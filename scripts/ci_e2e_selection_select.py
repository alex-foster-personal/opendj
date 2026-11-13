"""Selection rules and budget fitting for the e2e spec selector."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from fnmatch import fnmatch
from pathlib import Path

from scripts.affected_tests import build_graph, reverse_reach
from scripts.ci_commented_list import read_commented_list
from scripts.ci_e2e_selection_constants import (
    ALL_GATES,
    ALWAYS_RUN_NAME,
    ENGINE_PATTERNS,
    ENGINE_PREFIXES,
    FRONTEND,
    FULL_PATTERNS,
    GATES_AFTER_THE_MAP,
    REASON_ALWAYS,
    REASON_CHANGED_SPEC,
    REASON_E2E_INPUT,
    REASON_ENGINE,
    SPECS_DIR,
    STATIC_GATES,
    STATIC_GATE_SPECS,
    STRONG_PREFIXES,
    FullSuite,
    Git,
)
from scripts.ci_e2e_selection_map import BudgetConfig, Footprints


def always_run_specs(root: Path) -> list[str]:
    """The spec paths `ci/e2e-always-run.txt` lists; a missing list is FullSuite."""
    try:
        return read_commented_list(root / ALWAYS_RUN_NAME)
    except FileNotFoundError as missing:
        raise FullSuite(f"the always-run list `{ALWAYS_RUN_NAME}` is missing") from missing


def _tree(git: Git, ref: str, prefix: str) -> set[str]:
    return set(git(["ls-tree", "-r", "--name-only", ref, "--", prefix]).split())


def engine_reach(changed_python: list[str], root: Path) -> list[str]:
    """Changed Python paths that the engine imports, transitively; raises on an unknown path."""
    importers, module_of = build_graph(root)
    by_path = {relative.as_posix(): name for name, relative in module_of.items()}  # git paths are posix
    unresolved = [path for path in changed_python if path not in by_path]
    if unresolved:
        raise FullSuite(
            "changed Python the import graph cannot place (deleted or outside a package): " + ", ".join(unresolved)
        )
    reached: list[str] = []
    for path in changed_python:
        seen = reverse_reach({by_path[path]}, importers)
        if any(name.startswith(ENGINE_PREFIXES) for name in seen):
            reached.append(path)
    return reached


@dataclass
class Item:
    """One candidate: a root spec or a named gate, with why and how it ranks."""

    kind: str  # "spec" or "gate"
    name: str
    reasons: list[str] = field(default_factory=list)
    overlap: int = 0
    #: Sum over the changed files it executed of 1 / (specs and gates that executed that
    #: file): a file only three specs load pulls those three far harder than a file every
    #: spec loads (347 of the 955 paths in the first map are loaded by 70 or more of the
    #: 85 root specs, so the overlap COUNT alone ties almost everything).
    specificity: float = 0.0
    seconds: float = 0.0

    @property
    def strong(self) -> bool:
        return any(reason.startswith(STRONG_PREFIXES) for reason in self.reasons)


@dataclass
class Selection:
    specs: dict[str, Item] = field(default_factory=dict)
    gates: dict[str, Item] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    #: Why every spec is a candidate, when the change reaches the engine.
    engine: str | None = None

    def spec(self, name: str, reason: str, footprints: Footprints) -> Item:
        item = self.specs.setdefault(name, Item("spec", name, seconds=footprints.seconds(name)))
        item.reasons.append(reason)
        return item

    def gate(self, name: str, reason: str, footprints: Footprints) -> Item:
        item = self.gates.setdefault(name, Item("gate", name, seconds=footprints.gate_seconds(name)))
        item.reasons.append(reason)
        return item


def _engine_reason(changed: Sequence[str], root: Path) -> str | None:
    """Why this is an engine change, or None; Python outside the engine's reach is a note."""
    for path in changed:
        for pattern, why in ENGINE_PATTERNS:
            if fnmatch(path, pattern):
                return f"{path} changed: {why}"
    python = sorted(path for path in changed if path.endswith(".py"))
    if not python:
        return None
    reached = engine_reach(python, root)
    return "the engine imports changed Python: " + ", ".join(reached) if reached else None


def _select_changed_spec(spec: str, footprints: Footprints, selection: Selection) -> None:
    """A changed spec runs itself in the root suite, and promotes the gate that owns it.

    A spec the root config ignores is not added to the root filter (it would match nothing);
    its owner, a footprinted gate by the map's `specs`, or a static gate or a gate the map is
    built before by `STATIC_GATE_SPECS` and `GATES_AFTER_THE_MAP`, is strong instead. A spec
    with none is a dedicated-config suite the gate never runs (the extended tier), noted.
    """
    owners = [gate for gate, entry in footprints.gates.items() if spec in entry["specs"]]
    owners += [
        gate
        for gate, prefixes in {**STATIC_GATE_SPECS, **GATES_AFTER_THE_MAP}.items()
        if spec.startswith(prefixes) and gate not in owners
    ]
    for gate in owners:
        selection.gate(gate, f"{REASON_CHANGED_SPEC} {spec} is one of its specs", footprints)
    if spec in footprints.ignored:
        if not owners:
            selection.notes.append(f"{spec}: changed, but the root config ignores it and no gate of this job runs it")
        return
    selection.spec(spec, REASON_CHANGED_SPEC, footprints)


def _select_engine(engine: str, footprints: Footprints, selection: Selection) -> None:
    """An engine change: every non-skipped spec and every footprinted gate, with no overlap."""
    selection.engine = engine
    reason = f"{REASON_ENGINE}: {engine}; no footprint tells the specs apart, the budget takes the cheapest"
    for spec, entry in footprints.specs.items():
        if entry["status"] != "skipped":
            selection.spec(spec, reason, footprints)
    for gate in footprints.gates:
        selection.gate(gate, reason, footprints)


def _full_reason(changed: Sequence[str]) -> str | None:
    for path in changed:
        for pattern, why in FULL_PATTERNS:
            if fnmatch(path, pattern):
                return f"{path} changed: {why}"
        if path.startswith(FRONTEND + SPECS_DIR) and not path.endswith(".spec.ts"):
            return f"{path} changed: {REASON_E2E_INPUT}"
    return None


def _select_src(path: str, footprints: Footprints, selection: Selection, git: Git, base: str, at_map: set[str]) -> None:
    """One changed `src/` file: its specs and gates, or a note, or FullSuite for a stale map."""
    local = path.removeprefix(FRONTEND)
    index = footprints.index(local)
    touching: list[Item] = []
    if index is not None:
        reason = f"footprint overlap: {local}"
        touching = [selection.spec(s, reason, footprints) for s, e in footprints.specs.items() if index in e["files"]]
        touching += [selection.gate(g, reason, footprints) for g, e in footprints.gates.items() if index in e["files"]]
    if touching:
        for item in touching:
            item.overlap += 1
            item.specificity += 1.0 / len(touching)
        return
    if path in at_map:
        selection.notes.append(f"{local}: in no spec's or gate's footprint at {footprints.sha[:9]}")
    elif path in _tree(git, base, path):
        raise FullSuite(
            f"{local} is on the base but was added after the map ({footprints.sha[:9]}); its footprint is unknown"
        )
    else:
        selection.notes.append(f"{local}: added by this change; its importers are selected")


def _select_unmeasured(
    footprints: Footprints, selection: Selection, git: Git, head: str, frontend_changed: bool
) -> None:
    for spec, entry in footprints.specs.items():
        if entry["status"] == "unmeasured":
            selection.spec(spec, "unmeasured: it failed or timed out when the map was built", footprints)
    on_disk = {p.removeprefix(FRONTEND) for p in _tree(git, head, FRONTEND + SPECS_DIR) if p.endswith(".spec.ts")}
    for spec in sorted(on_disk - set(footprints.specs) - footprints.ignored):
        selection.spec(spec, f"unmeasured: added after the map ({footprints.sha[:9]})", footprints)
    for gate in ALL_GATES:
        gate_entry = footprints.gates.get(gate)
        if gate_entry is not None and gate_entry["status"] != "unmeasured":
            continue
        why = (
            "no request log can footprint it"
            if gate in STATIC_GATES
            else "absent from the map or failed when it was built"
        )
        if frontend_changed:
            selection.gate(gate, f"unmeasured: {why}", footprints)
        else:
            selection.notes.append(f"gate {gate}: {why}, and nothing under the frontend changed")


def select_items(
    changed: Sequence[str], footprints: Footprints, *, root: Path, base: str, head: str, git: Git
) -> Selection:
    """The candidate specs and gates with their reasons, or FullSuite."""
    full = _full_reason(changed)
    if full is not None:
        raise FullSuite(full)
    selection = Selection()
    engine = _engine_reason(changed, root)
    if engine is not None:
        _select_engine(engine, footprints, selection)
    src_changed = any(p.startswith(FRONTEND + "src/") for p in changed)
    at_map = _tree(git, footprints.sha, FRONTEND + "src") if src_changed else set()
    for path in changed:
        if path.startswith(FRONTEND + SPECS_DIR) and path.endswith(".spec.ts"):
            _select_changed_spec(path.removeprefix(FRONTEND), footprints, selection)
        elif path.startswith(FRONTEND + "src/"):
            _select_src(path, footprints, selection, git, base, at_map)
        elif path.endswith(".py"):
            if engine is None:
                selection.notes.append(f"{path}: Python outside the engine's reach")
        else:
            selection.notes.append(f"{path}: outside the root suite's reach")
    frontend_changed = engine is not None or any(p.startswith(FRONTEND) for p in changed)
    _select_unmeasured(footprints, selection, git, head, frontend_changed)
    for spec in always_run_specs(root):
        selection.spec(spec, REASON_ALWAYS, footprints)
    return selection


# ---------------------------------------------------------------------------
# the budget
# ---------------------------------------------------------------------------


@dataclass
class Fit:
    budget_min: float
    context: str
    planned_before_min: float
    planned_min: float
    over_budget: bool
    kept: list[str]
    dropped: list[dict]

    def as_record(self) -> dict:
        return asdict(self)


def _cost_min(item: Item, config: BudgetConfig) -> float:
    if item.kind == "gate" and item.name in STATIC_GATES:
        return config.static_gates_min[item.name]
    # A dev-server gate always pays its engine boot, measured or not (Sentry, PR #7948).
    overhead = config.gate_overhead_min if item.kind == "gate" else 0.0
    return overhead + item.seconds / 60.0 * config.min_per_recorded_minute


def _rank(item: Item) -> tuple[int, int, float, float, str]:
    """Overlap count first (the maintainer's order), then specificity, then recorded seconds ascending;
    an unmeasured item (no overlap) ranks after every overlap."""
    return (0 if item.overlap > 0 else 1, -item.overlap, -round(item.specificity, 6), item.seconds, item.name)


def fit(selection: Selection, config: BudgetConfig, budget_min: float, context: str) -> Fit:
    """Keep the strong items, then the weak ones in rank order while they fit the budget.

    A weak item that does not fit is dropped and the walk continues: a cheaper, lower-ranked
    item may still fit. The dropped items leave the selection here, so the record and the
    outputs agree.
    """
    items = [*selection.specs.values(), *selection.gates.values()]
    strong = [item for item in items if item.strong]
    weak = sorted((item for item in items if not item.strong), key=_rank)
    planned = config.root_overhead_min + sum(_cost_min(item, config) for item in strong)
    before = planned + sum(_cost_min(item, config) for item in weak)
    kept, dropped = [f"{item.kind} {item.name}" for item in strong], []
    for item in weak:
        cost = _cost_min(item, config)
        if planned + cost <= budget_min:
            planned += cost
            kept.append(f"{item.kind} {item.name}")
            continue
        dropped.append(
            {
                "item": f"{item.kind} {item.name}",
                "overlap": item.overlap,
                "specificity": round(item.specificity, 4),
                "seconds": item.seconds,
                "reasons": item.reasons,
            }
        )
        if item.kind == "spec":
            del selection.specs[item.name]
        else:
            del selection.gates[item.name]
    return Fit(
        budget_min=budget_min,
        context=context,
        planned_before_min=round(before, 2),
        planned_min=round(planned, 2),
        over_budget=planned > budget_min,
        kept=sorted(kept),
        dropped=dropped,
    )


def summary_line(result: Fit) -> str:
    state = "OVER BUDGET: strong items alone exceed it" if result.over_budget else "within budget"
    specs = sum(1 for d in result.dropped if d["item"].startswith("spec "))
    gates = len(result.dropped) - specs
    seconds = sum(d["seconds"] for d in result.dropped)
    return (
        f"E2E BUDGET ({result.context}): {result.budget_min:g} runner-min; planned {result.planned_before_min:g} -> "
        f"{result.planned_min:g} min, dropped {specs} spec(s) and {gates} gate(s) ({seconds:g} recorded-s); {state}"
    )
