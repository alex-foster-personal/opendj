"""Which root Playwright specs and named gates a pull request HEAD runs in the e2e gate
(TEST-BUDGET round 3).

The "Root Playwright suite (real engine + Vite)" step of `e2e.yml`'s gate ran every spec
the root config collects on every in-scope PR, about 9 of the gate's 22 minutes, and the
named gate steps around it (rekordbox, comment-hotkey, autoplay, headphone, library jobs,
preflight, webkit, deck-load smoke, stretch, master12, onboarding, the two Cmd K gates) ran
whatever changed. A
static route-to-source closure cannot narrow it: 199 of about 270 `page.goto` calls target
`/performance`, whose page reaches 759 of the 1172 source files. So the narrowing is test
impact analysis from MEASURED footprints: a map of which `src/` files each spec and each
named gate fetched from the dev server during a full run
(`apps/webui/frontend/vite-e2e-footprints.ts`, `tests/e2e/support/footprint-reporter.ts`,
built and published by `scripts/e2e_footprints_refresh.py`), ranked and fitted into a
runner-minute budget (`ci/e2e-budget.json`; the maintainer, Fri 30 Oct 2026: a hard cap per run,
the e2e share about 4 minutes on a frontend PR). Decision record:
(private path); rule: (private path).

Mode, decided once in the scope job and read by the gate's steps:

    full      the root step runs today's invocation and every named gate runs, unchanged.
    affected  the root step appends the selected spec paths as positional arguments, and a
              named gate step runs only when its name is in `e2e_gates`.

Candidates, each with a reason (the record names every rule that fired):
  - footprint overlap: a changed `apps/webui/frontend/src/` file is in the spec's or gate's
    footprint; the overlap COUNT (changed files it executed) is its rank.
  - changed spec: a changed `tests/e2e/*.spec.ts` runs itself; a gate whose collected specs
    include it runs. Strong: never dropped by the budget.
  - always run: the smoke floor `ci/e2e-always-run.txt`. Strong.
  - unmeasured: a spec the map saw fail or time out, a spec on disk the map never collected,
    a dev-server gate absent from the map, and the static gates (webkit, deck-load smoke,
    stretch, master12, onboarding, the two Cmd K gates, which no request log can footprint) when any file under
    the frontend changed. Ranked after every overlap; a static gate costs nothing modeled.

The budget (`fit`): planned minutes = root overhead + root spec seconds, plus per kept gate
its overhead + seconds. Strong items are priced first; the weak candidates are taken in
rank order (overlap descending, recorded seconds ascending) while they fit; the rest are
listed under `budget.dropped` so an escape can be attributed. Strong items alone over the
budget run anyway and the record says `over_budget`.

Falls back to FULL, never to a smaller run, and names the cause, when:
  - the switch repo variable `CI_E2E_SPEC_SELECTION` is set to anything but `affected`
    (unset or empty means on; `full` is the written kill switch);
  - a changed path is global (`FULL_PATTERNS`: the e2e support and fixture trees, any
    Playwright or Vite config, the frontend's package manifest or lockfile, the engine
    the suite boots, the map, the budget, this selector), or is any other non-spec file
    under the e2e tree (an allow list a spec reads has no footprint naming its reader);
  - a changed Python module is not in the import graph at all (deleted, or outside a
    package); a module the engine imports, a file under `apps/webui/server/`, or the
    Python dependency manifest or lock the gate installs from, is instead an ENGINE
    change: every spec and footprinted gate becomes a candidate with no overlap, so the
    budget takes the cheapest (the maintainer's hard cap);
  - a changed `src/` file is in no footprint AND existed on the base but not at the map's
    SHA (added after the map was built: the map is stale for it);
  - the map is missing, malformed, older than `MAX_MAP_AGE_DAYS`, or built at a SHA that
    is not an ancestor of the merge commit; the budget config is missing or malformed;
  - the selection cannot be computed for any reason at all.

Usage (the scope job of .github/workflows/e2e.yml):
    python3 -m scripts.ci_e2e_selection select --published MAP --out-dir DIR
    python3 -m scripts.ci_e2e_selection replay --merges 150 [--map MAP] [--budget-minutes 4]

`select` prints exactly one line on stdout, the mode, writes DIR/e2e-selection.json and
DIR/e2e-specs.txt, and appends `e2e_mode`, `e2e_specs` and `e2e_gates` to $GITHUB_OUTPUT.
Everything for humans goes to stderr and the job summary. Standard library only: the
scope job runs on the runner image's own `python3`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import traceback
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from fnmatch import fnmatch
from pathlib import Path

from scripts.affected_tests import REPO, build_graph, reverse_reach
from scripts.ci_required_gate import merge_commit_changed_files
from scripts.e2e_footprints_refresh import GATES

SWITCH_VARIABLE = "CI_E2E_SPEC_SELECTION"
SWITCH_ON = "affected"
SCHEMA = 2
RECORD_NAME = "e2e-selection.json"
SPECS_NAME = "e2e-specs.txt"
FRONTEND = "apps/webui/frontend/"
SPECS_DIR = "tests/e2e/"
SEED_MAP = FRONTEND + SPECS_DIR + ".spec_footprints.json"
ALWAYS_RUN_NAME = "ci/e2e-always-run.txt"
BUDGET_NAME = "ci/e2e-budget.json"
MAX_MAP_AGE_DAYS = 7
#: Python packages the root suite boots: a changed module any of them imports is FULL.
ENGINE_PREFIXES = ("apps.engine_core", "apps.webui.server")
#: Named gates no request log can footprint: production builds, a no-server spec, a shell
#: script, a Python helper that owns its own fixture. They run on any frontend change and are priced at zero (unknown) minutes.
STATIC_GATES = ("webkit", "deckload-smoke", "stretch", "master12", "onboarding", "cmdk-pick-race", "cmdk-preview-cue")
#: Which specs each static gate runs (their own configs, root testIgnore in
#: playwright.config.ts): a changed one of these promotes its gate to strong, since the root
#: filter would match nothing for it. Prefixes, relative to the frontend.
STATIC_GATE_SPECS: dict[str, tuple[str, ...]] = {
    "webkit": (
        "tests/e2e/webkit-deckload.spec.ts",
        "tests/e2e/playlist-detail.spec.ts",
        "tests/e2e/setup-entry-points.spec.ts",
    ),
    "deckload-smoke": ("tests/e2e/deckload-smoke.spec.ts",),
    "stretch": ("tests/e2e/stretch-quality.spec.ts",),
    "master12": ("tests/e2e/audio-output-topology.spec.ts",),
    "onboarding": ("tests/e2e/onboarding-gauntlet-",),
    "cmdk-pick-race": ("tests/e2e/cmdk-pick-race.spec.ts",),
    "cmdk-preview-cue": ("tests/e2e/cmdk-preview-cue.spec.ts",),
}
ALL_GATES = tuple(sorted(GATES)) + STATIC_GATES
HEX40 = re.compile(r"^[0-9a-f]{40}$")
REASON_ALWAYS = "always run (ci/e2e-always-run.txt)"
REASON_CHANGED_SPEC = "changed spec"
STRONG_PREFIXES = (REASON_ALWAYS, REASON_CHANGED_SPEC)

#: A change to any of these runs the full root suite. `*` crosses `/` (fnmatch).
FULL_PATTERNS: tuple[tuple[str, str], ...] = (
    (FRONTEND + "tests/e2e/support/*", "the shared e2e support code and fixtures builders"),
    (FRONTEND + "tests/e2e/fixtures/*", "the shared e2e fixtures"),
    (FRONTEND + "tests/e2e/*.config.ts", "a Playwright or Vite config of the e2e tree"),
    (FRONTEND + "tests/e2e/*-endpoints.ts", "an engine endpoint helper the e2e configs share"),
    (FRONTEND + "playwright*.config.ts", "the root Playwright config"),
    (FRONTEND + "vite*.ts", "the Vite config or a Vite plugin every page is served through"),
    (FRONTEND + "svelte.config.js", "the SvelteKit config"),
    (FRONTEND + "tsconfig*.json", "the TypeScript config the dev server compiles with"),
    (FRONTEND + "webui-port-config.ts", "the port claim every suite boots through"),
    (FRONTEND + "package.json", "the frontend's package manifest"),
    (FRONTEND + "pnpm-lock.yaml", "the frontend's dependency lock"),
    (FRONTEND + "static/*", "a static asset served outside the module graph"),
    (FRONTEND + "src/app.html", "the document every route is served in"),
    (FRONTEND + "src/service-worker*", "the service worker"),
    # Server-executed SvelteKit modules run inside the dev server, so no browser request
    # ever records them in a footprint: a change there is FULL, not unrelated (Codex,
    # PR #7948: `hooks.server.ts` injects the full-reload receiver a root spec tests).
    (FRONTEND + "src/hooks.server.*", "a server hook every dev-server response runs through"),
    (FRONTEND + "src/*.server.ts", "a server-only module no browser request records"),
    (FRONTEND + "src/*/+server.ts", "a server endpoint no browser request records"),
    (SEED_MAP, "the footprint map itself"),
    (ALWAYS_RUN_NAME, "the always-run list"),
    (BUDGET_NAME, "the e2e budget"),
    ("scripts/ci_e2e_selection.py", "this selector"),
    ("scripts/e2e_footprints_refresh.py", "the map builder"),
    ("scripts/affected_tests.py", "the import graph this selector reads the engine's reach from"),
    ("scripts/ci_required_gate.py", "the scope decision this selector runs inside"),
)

#: A change here is an ENGINE change: every spec and gate is a candidate with no footprint
#: to tell them apart, and the budget takes the cheapest (the maintainer's hard cap, Fri 30 Oct 2026;
#: before it these were FULL, which the replay measured as 62 of 65 FULL merges in 150).
ENGINE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("apps/webui/server/*", "the engine every spec boots"),
    ("apps/webui/*.py", "the webui package root (the port claim, the server entry)"),
    # The gate installs the engine's dependencies with `uv sync --locked` from these two, so
    # a dependency update changes the engine every API-backed spec talks to (Codex, PR #7948).
    ("pyproject.toml", "the Python dependency manifest the gate installs the engine from"),
    ("uv.lock", "the Python dependency lock the gate installs the engine from"),
)
#: A file under the e2e tree that is not a spec (an allow list a spec reads, a timeouts
#: module a config imports) has no footprint and no import edge the selector can see, so a
#: change to one is FULL rather than a note: the audit a changed allow list feeds must run.
REASON_E2E_INPUT = "an input of the e2e tree outside a spec (no footprint names its reader)"
REASON_ENGINE = "engine change"

#: One shell seam: `git -C <repo> <args...>` -> stdout. Tests replace it.
Git = Callable[[Sequence[str]], str]


class FullSuite(Exception):
    """The change set cannot be narrowed; the message says why."""


# ---------------------------------------------------------------------------
# the map and the budget
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Footprints:
    sha: str
    built: datetime
    paths: tuple[str, ...]
    ignored: frozenset[str]
    specs: dict[str, dict]
    gates: dict[str, dict]
    source: str

    def index(self, path: str) -> int | None:
        try:
            return self.paths.index(path)
        except ValueError:
            return None

    def seconds(self, spec: str) -> float:
        return float(self.specs.get(spec, {}).get("seconds", 0.0))

    def gate_seconds(self, gate: str) -> float:
        return float(self.gates.get(gate, {}).get("seconds", 0.0))


def _check_entry(label: str, name: str, entry: object, paths: list[str]) -> None:
    if not isinstance(entry, dict) or entry.get("status") not in {"passed", "skipped", "unmeasured"}:
        raise FullSuite(f"{label}: {name} has no status of passed, skipped or unmeasured")
    files = entry.get("files")
    if not isinstance(files, list) or any(not isinstance(i, int) or not 0 <= i < len(paths) for i in files):
        raise FullSuite(f"{label}: {name} has a file index outside paths")
    if not isinstance(entry.get("seconds", 0.0), (int, float)):
        raise FullSuite(f"{label}: {name} has a non-numeric seconds")


def validate_map(raw: object, label: str) -> Footprints:
    """A map that can be selected over, or FullSuite naming what is wrong with it."""
    if not isinstance(raw, dict) or raw.get("schema") != 1:
        raise FullSuite(f"{label}: not a schema 1 footprint map")
    sha, built, paths = raw.get("sha"), raw.get("built"), raw.get("paths")
    specs, ignored, gates = raw.get("specs"), raw.get("ignored", []), raw.get("gates", {})
    if not isinstance(sha, str) or not HEX40.match(sha):
        raise FullSuite(f"{label}: sha {sha!r} is not a 40-hex commit")
    try:
        built_at = datetime.fromisoformat(str(built))
    except ValueError as exc:
        raise FullSuite(f"{label}: built {built!r} is not an ISO-8601 timestamp") from exc
    if built_at.tzinfo is None:
        raise FullSuite(f"{label}: built {built!r} carries no timezone")
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        raise FullSuite(f"{label}: paths is not a list of strings")
    if not isinstance(specs, dict) or not specs:
        raise FullSuite(f"{label}: specs is not a non-empty object")
    if not isinstance(ignored, list) or not all(isinstance(p, str) for p in ignored):
        raise FullSuite(f"{label}: ignored is not a list of strings")
    if not isinstance(gates, dict):
        raise FullSuite(f"{label}: gates is not an object")
    for spec, entry in specs.items():
        _check_entry(label, f"spec {spec}", entry, paths)
    for gate, entry in gates.items():
        _check_entry(label, f"gate {gate}", entry, paths)
        if gate not in GATES or not isinstance(entry.get("specs"), list):
            raise FullSuite(f"{label}: gate {gate} is not a footprinted gate with a specs list")
    return Footprints(sha, built_at, tuple(paths), frozenset(ignored), specs, gates, label)


def load_map(published: Path | None, seed: Path) -> Footprints:
    """The published map when it exists and validates, else the committed seed."""
    if published is not None and published.exists():
        return validate_map(json.loads(published.read_text(encoding="utf-8")), f"published map {published}")
    if not seed.exists():
        raise FullSuite(f"no footprint map: {seed} is missing and nothing was published")
    return validate_map(json.loads(seed.read_text(encoding="utf-8")), f"committed seed {seed}")


def check_fresh(footprints: Footprints, now: datetime, head: str, git: Git) -> None:
    age_days = (now - footprints.built).total_seconds() / 86400
    if age_days > MAX_MAP_AGE_DAYS:
        raise FullSuite(
            f"the footprint map ({footprints.source}, {footprints.sha[:9]}) is {age_days:.1f} days old, "
            f"over the {MAX_MAP_AGE_DAYS}-day limit; refresh it (scripts/e2e_footprints_refresh.py)"
        )
    try:
        git(["merge-base", "--is-ancestor", footprints.sha, head])
    except subprocess.CalledProcessError as exc:
        raise FullSuite(
            f"the footprint map's sha {footprints.sha[:9]} is not an ancestor of the merge commit {head}; "
            "the map was built on another line of history"
        ) from exc


@dataclass(frozen=True)
class BudgetConfig:
    minutes: dict[str, float]
    root_overhead_min: float
    gate_overhead_min: float
    min_per_recorded_minute: float
    #: Estimated minutes per static gate (no footprint, no recorded seconds), so an unmeasured
    #: gate is never free to the budget.
    static_gates_min: dict[str, float]

    def for_context(self, queue_draft: bool) -> float:
        return self.minutes["queue_draft" if queue_draft else "pr_head"]


def load_budget(path: Path) -> BudgetConfig:
    """`ci/e2e-budget.json`; missing or malformed is FullSuite, never a silent "no budget"."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as missing:
        raise FullSuite(f"the e2e budget `{path}` is missing") from missing
    except ValueError as bad:
        raise FullSuite(f"the e2e budget `{path}` is not valid JSON: {bad}") from bad
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise FullSuite(f"the e2e budget `{path}` must be an object with `version: 1`")
    try:
        model = raw["model"]
        config = BudgetConfig(
            minutes={context: float(raw["runner_minutes"][context]) for context in ("pr_head", "queue_draft")},
            root_overhead_min=float(model["root_overhead_min"]),
            gate_overhead_min=float(model["gate_overhead_min"]),
            min_per_recorded_minute=float(model["min_per_recorded_minute"]),
            static_gates_min={gate: float(raw["static_gates_min"][gate]) for gate in STATIC_GATES},
        )
    except (KeyError, TypeError, ValueError) as bad:
        raise FullSuite(f"the e2e budget `{path}` is missing or mistypes a field: {bad!r}") from bad
    if any(m <= 0 for m in config.minutes.values()):
        raise FullSuite(f"the e2e budget `{path}`: runner minutes must be positive")
    return config


# ---------------------------------------------------------------------------
# the rules
# ---------------------------------------------------------------------------


def always_run_specs(root: Path) -> list[str]:
    """The spec paths `ci/e2e-always-run.txt` lists; a missing list is FullSuite."""
    try:
        text = (root / ALWAYS_RUN_NAME).read_text(encoding="utf-8")
    except FileNotFoundError as missing:
        raise FullSuite(f"the always-run list `{ALWAYS_RUN_NAME}` is missing") from missing
    entries = (line.split("#", 1)[0].strip() for line in text.splitlines())
    return [entry for entry in entries if entry]


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
    its owner, a footprinted gate by the map's `specs` or a static gate by
    `STATIC_GATE_SPECS`, is strong instead. A spec with neither is a dedicated-config suite
    the gate never runs (the extended tier), noted.
    """
    owners = [gate for gate, entry in footprints.gates.items() if spec in entry["specs"]]
    owners += [gate for gate, prefixes in STATIC_GATE_SPECS.items() if spec.startswith(prefixes)]
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


# ---------------------------------------------------------------------------
# the decision and its record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Record:
    schema: int
    mode: str
    switch: str | None
    reason: str
    changed_files: list[str]
    map_sha: str
    map_built: str
    map_source: str
    universe_specs: int
    universe_seconds: float
    selected: dict[str, list[str]]
    selected_seconds: float
    gates: dict[str, list[str]]
    budget: dict | None
    notes: list[str]


@dataclass(frozen=True)
class Revisions:
    """The merge commit's base and head, whether the map's age and ancestry are checked, and
    the budget context (a Trunk queue draft's head ref starts with `trunk-merge/`)."""

    base: str = "HEAD^1"
    head: str = "HEAD"
    fresh: bool = True
    queue_draft: bool = False


#: The pull_request checkout: GitHub's test merge commit against its base parent.
MERGE_COMMIT = Revisions()


def _git_in(repo: Path) -> Git:
    """`git -C repo ...` by resolved path (AGENTS.md, "Portable by default"); no git is FullSuite."""
    executable = shutil.which("git")
    if executable is None:
        raise FullSuite("git is not on PATH, so the change set and the map's ancestry cannot be read")

    def git(args: Sequence[str]) -> str:
        return subprocess.run([executable, "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout

    return git


def _narrow(
    switch: str | None,
    changed: Sequence[str],
    published: Path | None,
    root: Path,
    git: Git,
    now: datetime,
    revisions: Revisions,
    budget_minutes: float | None,
) -> tuple[Footprints, Selection, Fit]:
    # An unset repo variable reaches the step as an empty string (`${{ vars.X }}`), which is
    # the documented default-on state, not the kill switch (Codex, PR #7948).
    if switch and switch != SWITCH_ON:
        raise FullSuite(f"{SWITCH_VARIABLE}={switch!r} (anything but {SWITCH_ON!r} or empty runs the full suite)")
    if not changed:
        raise FullSuite("the merge commit changes no files; an empty diff is unmeasured")
    footprints = load_map(published, root / SEED_MAP)
    config = load_budget(root / BUDGET_NAME)
    if revisions.fresh:
        # The merge commit, not the base: a map built on main is behind both, and the
        # committed seed is built on this PR's own branch, which only the head contains.
        check_fresh(footprints, now, revisions.head, git)
    selection = select_items(changed, footprints, root=root, base=revisions.base, head=revisions.head, git=git)
    context = "queue_draft" if revisions.queue_draft else "pr_head"
    budget = config.for_context(revisions.queue_draft) if budget_minutes is None else budget_minutes
    return footprints, selection, fit(selection, config, budget, context)


def decide(
    switch: str | None,
    changed: Sequence[str],
    *,
    published: Path | None,
    root: Path,
    git: Git,
    now: datetime,
    revisions: Revisions = MERGE_COMMIT,
    budget_minutes: float | None = None,
) -> Record:
    """The whole decision as a record; never raises for a selection problem (that is FULL)."""
    footprints: Footprints | None = None
    budget: dict | None = None
    try:
        try:
            footprints, selection, fitted = _narrow(
                switch, changed, published, root, git, now, revisions, budget_minutes
            )
        except FullSuite:
            raise
        except Exception as exc:
            raise FullSuite("the selection could not be computed: " + traceback.format_exc()) from exc
        mode = "affected"
        engine = f" ({REASON_ENGINE}: {selection.engine})" if selection.engine else ""
        reason = (
            f"{len(selection.specs)} spec(s) and {len(selection.gates)} gate(s) selected from "
            f"{footprints.source}{engine}; {summary_line(fitted)}"
        )
        selected = {name: selection.specs[name].reasons for name in sorted(selection.specs)}
        gates = {name: selection.gates[name].reasons for name in sorted(selection.gates)}
        budget, notes = fitted.as_record(), selection.notes
    except FullSuite as full:
        mode, reason, selected, gates, notes = "full", str(full), {}, {}, []
        if footprints is None:
            footprints = _map_if_loadable(published, root)
    universe = len(footprints.specs) if footprints else 0
    universe_seconds = sum(footprints.seconds(s) for s in footprints.specs) if footprints else 0.0
    selected_seconds = sum(footprints.seconds(s) for s in selected) if footprints else 0.0
    return Record(
        SCHEMA,
        mode,
        switch,
        reason,
        list(changed),
        footprints.sha if footprints else "",
        footprints.built.isoformat() if footprints else "",
        footprints.source if footprints else "",
        universe,
        round(universe_seconds, 1),
        selected,
        round(selected_seconds, 1),
        gates,
        budget,
        notes,
    )


def _map_if_loadable(published: Path | None, root: Path) -> Footprints | None:
    """The map for the record's universe figures when FULL was decided before or without one."""
    try:
        return load_map(published, root / SEED_MAP)
    except (FullSuite, OSError, ValueError):
        return None


def gates_output(record: Record) -> str:
    """` a b ` with a space on both ends, so a step's `contains(..., ' a ')` is exact."""
    names = ALL_GATES if record.mode == "full" else tuple(record.gates)
    return f" {' '.join(names)} " if names else " "


def report_lines(record: Record) -> list[str]:
    lines = [f"[e2e-selection] mode={record.mode}: {record.reason}"]
    if record.mode == "affected":
        share = record.universe_specs and len(record.selected) / record.universe_specs
        lines.append(
            f"[e2e-selection] selected {len(record.selected)} of {record.universe_specs} root specs "
            f"({share:.0%}), {record.selected_seconds:.0f} of {record.universe_seconds:.0f} recorded seconds; "
            f"gates: {', '.join(record.gates) or 'none'}"
        )
        lines.extend(f"[e2e-selection]   {spec}  <- {'; '.join(reasons)}" for spec, reasons in record.selected.items())
        lines.extend(
            f"[e2e-selection]   gate {gate}  <- {'; '.join(reasons)}" for gate, reasons in record.gates.items()
        )
        if record.budget:
            lines.extend(
                f"[e2e-selection]   dropped by the budget: {d['item']} (overlap {d['overlap']}, {d['seconds']:g} s)"
                for d in record.budget["dropped"]
            )
    lines.extend(f"[e2e-selection]   note: {note}" for note in record.notes)
    return lines


def summary_markdown(record: Record) -> str:
    rows = [
        "### Root Playwright spec selection (TEST-BUDGET round 3)",
        "",
        "| | |",
        "|:--|:--|",
        f"| mode | `{record.mode}` |",
        f"| reason | {record.reason.splitlines()[0]} |",
        f"| footprint map | `{record.map_sha[:9]}` built {record.map_built} ({record.map_source}) |",
        f"| selected specs | {len(record.selected)} of {record.universe_specs} |",
        f"| recorded seconds | {record.selected_seconds:.0f} of {record.universe_seconds:.0f} |",
        f"| named gates | {', '.join(record.gates) if record.mode == 'affected' else 'all'} |",
        "",
    ]
    if record.mode == "affected":
        rows.append("<details><summary>selected specs and gates</summary>\n")
        rows += [f"- `{spec}`: {'; '.join(reasons)}" for spec, reasons in record.selected.items()]
        rows += [f"- gate `{gate}`: {'; '.join(reasons)}" for gate, reasons in record.gates.items()]
        if record.budget and record.budget["dropped"]:
            rows.append("\nDropped by the budget:\n")
            rows += [f"- `{d['item']}` (overlap {d['overlap']}, {d['seconds']:g} s)" for d in record.budget["dropped"]]
        rows.append("\n</details>")
    return "\n".join(rows) + "\n"


def _append(path_env: str, text: str) -> None:
    target = os.environ.get(path_env)
    if target:
        with open(target, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(text)


def _select(arguments: argparse.Namespace) -> int:
    root = Path(arguments.root).resolve()
    changed = merge_commit_changed_files(root)
    queue_draft = os.environ.get("GITHUB_HEAD_REF", "").startswith("trunk-merge/")
    record = decide(
        os.environ.get(SWITCH_VARIABLE),
        changed,
        published=Path(arguments.published) if arguments.published else None,
        root=root,
        git=_git_in(root),
        now=datetime.now(UTC),
        revisions=Revisions(queue_draft=queue_draft),
    )
    out_dir = Path(arguments.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / RECORD_NAME).write_text(json.dumps(asdict(record), indent=2) + "\n", encoding="utf-8", newline="\n")
    specs = " ".join(record.selected) if record.mode == "affected" else ""
    (out_dir / SPECS_NAME).write_text(specs + "\n", encoding="utf-8", newline="\n")
    for line in report_lines(record):
        print(line, file=sys.stderr)
    _append("GITHUB_STEP_SUMMARY", summary_markdown(record))
    _append("GITHUB_OUTPUT", f"e2e_mode={record.mode}\ne2e_specs={specs}\ne2e_gates={gates_output(record)}\n")
    print(record.mode)
    return 0


# ---------------------------------------------------------------------------
# replay: the measurement
# ---------------------------------------------------------------------------


def _percentile(values: list[float], share: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(share * (len(ordered) - 1)))] if ordered else 0.0


def _share_row(label: str, values: list[float]) -> str:
    if not values:
        return f"| {label} | n/a |"
    return (
        f"| {label} | median {_percentile(values, 0.5):.0%}, p90 {_percentile(values, 0.9):.0%}, "
        f"mean {sum(values) / len(values):.0%} (n={len(values)}) |"
    )


def _dropped_specs(record: Record) -> int:
    return sum(1 for d in (record.budget or {}).get("dropped", []) if d["item"].startswith("spec "))


def replay(
    root: Path, ref: str, merges: int, published: Path | None, git: Git, now: datetime, budget_minutes: float | None
) -> str:
    """Replay the selector over the last `merges` first-parent merges of `ref`, as a report.

    Measures the steady state: the map is taken as fresh for every merge (`fresh=False`
    skips the age and ancestry checks, which a map built today fails for every past base),
    and the Python import graph is today's. Each merge's change set is its diff against its
    first parent, which is what the scope job sees on the PR's test-merge commit. Only
    merges the e2e scope would have run are counted (`scripts.ci_pr_scope`).
    """
    from scripts.ci_pr_scope import E2E_PATHS_IGNORE, workflow_would_run_for_files

    shas = git(["log", "--merges", "--first-parent", "--format=%H", "-n", str(merges), ref]).split()
    rows: list[Record] = []
    shas_in_scope: list[str] = []
    out_of_scope = 0
    for sha in shas:
        changed = [line for line in git(["diff", "--no-renames", "--name-only", f"{sha}^1", sha]).split("\n") if line]
        if not workflow_would_run_for_files(changed, paths_ignore=E2E_PATHS_IGNORE):
            out_of_scope += 1
            continue
        revisions = Revisions(f"{sha}^1", sha, fresh=False)
        shas_in_scope.append(sha)
        rows.append(
            decide(
                None,
                changed,
                published=published,
                root=root,
                git=git,
                now=now,
                revisions=revisions,
                budget_minutes=budget_minutes,
            )
        )
    universe = max((r.universe_specs for r in rows), default=0) or 1
    narrowed = [r for r in rows if r.mode == "affected"]
    full = [r.reason.splitlines()[0] for r in rows if r.mode == "full"]
    kept_share = [len(r.selected) / universe if r.mode == "affected" else 1.0 for r in rows]
    before_share = [(len(r.selected) + _dropped_specs(r)) / universe if r.mode == "affected" else 1.0 for r in rows]
    seconds_share = [
        r.selected_seconds / r.universe_seconds if r.mode == "affected" and r.universe_seconds else 1.0 for r in rows
    ]
    planned = [r.budget["planned_min"] for r in narrowed if r.budget]
    gates_kept = [len(r.gates) / len(ALL_GATES) if r.mode == "affected" else 1.0 for r in rows]
    over = sum(1 for r in narrowed if r.budget and r.budget["over_budget"])
    causes: dict[str, int] = {}
    for reason in full:
        key = reason.split(" changed: ", 1)[1] if " changed: " in reason else reason.split(":", 1)[0]
        causes[key] = causes.get(key, 0) + 1
    budget_label = f"{budget_minutes:g}" if budget_minutes is not None else "ci/e2e-budget.json"
    lines = [
        f"replayed {len(shas)} first-parent merges of {ref}: {len(rows)} in e2e scope, {out_of_scope} out of scope (no gate run)",
        f"universe: {universe} root specs, {len(ALL_GATES)} named gates; budget {budget_label} runner-min",
        "",
        "| measure (in-scope merges; a FULL merge counts as 100%) | value |",
        "|:--|:--|",
        f"| FULL fraction | {len(full)}/{len(rows)} = {len(full) / len(rows):.0%} |"
        if rows
        else "| FULL fraction | n/a |",
        _share_row("root spec share kept after the budget", kept_share),
        _share_row("root spec share by overlap before the budget", before_share),
        _share_row("root spec share kept, narrowed merges only", [len(r.selected) / universe for r in narrowed]),
        _share_row("recorded-seconds share kept", seconds_share),
        _share_row(f"named gates kept (of {len(ALL_GATES)})", gates_kept),
        (
            f"| planned minutes, narrowed merges | median {_percentile(planned, 0.5):g}, p90 {_percentile(planned, 0.9):g} |"
            if planned
            else "| planned minutes | n/a |"
        ),
        f"| over budget on strong items alone | {over}/{len(narrowed)} |",
        "",
        "FULL causes:",
    ]
    lines += [f"- {count} x {cause}" for cause, count in sorted(causes.items(), key=lambda kv: -kv[1])]
    lines += ["", "per merge (sha, mode, kept (private path), gates, planned min, first reason):"]
    for sha, record in zip(shas_in_scope, rows, strict=True):
        planned_min = record.budget["planned_min"] if record.budget else 0.0
        lines.append(
            f"- {sha[:10]} {record.mode} {len(record.selected)}/{universe} g={len(record.gates)} "
            f"{planned_min:g}min {record.reason.splitlines()[0][:100]}"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    select = commands.add_parser("select", help="decide the root suite's mode, specs and gates")
    select.add_argument("--published", help="the fetched ci-data-e2e map; absent means the committed seed")
    select.add_argument("--out-dir", required=True)
    select.add_argument("--root", default=str(REPO))
    rerun = commands.add_parser("replay", help="measure the selection over past merges (the map taken as fresh)")
    rerun.add_argument("--merges", type=int, default=150)
    rerun.add_argument("--ref", default="origin/main")
    rerun.add_argument("--map", help="a map to replay with; absent means the committed seed")
    rerun.add_argument("--budget-minutes", type=float, help="override the budget for a sensitivity run")
    rerun.add_argument("--root", default=str(REPO))
    arguments = parser.parse_args(argv)
    if arguments.command == "select":
        return _select(arguments)
    if arguments.command == "replay":
        root = Path(arguments.root).resolve()
        published = Path(arguments.map) if arguments.map else None
        print(
            replay(
                root,
                arguments.ref,
                arguments.merges,
                published,
                _git_in(root),
                datetime.now(UTC),
                arguments.budget_minutes,
            )
        )
        return 0
    parser.error(f"unknown command {arguments.command!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
