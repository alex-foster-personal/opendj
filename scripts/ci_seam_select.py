"""Module-level pytest selection for a webui-only change set (SMARTEST-CI round 14,
docs/decisions/ADR-NEW-seam-ci.md; round 15 own-ancestor initializer edges).

A frontend change cannot reach Python through an import, only through code that reads
frontend files. The webui_frontend scope in `ci/test-scopes.yml` declares the strings
such code spells (`mentions`). So the sound selection for a webui-only diff is:

- every tracked Python file whose code literals mention the frontend (`mentioned_strings`,
  the planner's own reader), as the SEED;
- every module whose import closure reaches a seed, read with `imported_modules` over
  `apps`, `scripts`, `ops` AND `tests` (a test helper is reached through `tests.` imports),
  where each import also pulls in the known parents of the imported name (their `__init__`
  runs first), and every tracked module also depends on its own strict-ancestor package
  `__init__.py` files (pytest runs those before collecting the module);
- every test module beneath a selected `conftest.py`, which pytest loads without an import;
- everything `always` matches.

The selection's test modules are what runs. Any other change set is `full`. Round 14 is
SHADOW: the `contracts` job logs the selection and nothing is skipped, so recall against
real failures can be counted before anything narrows.

Every read fails LOUD (`PlanError`): a source the selector cannot parse is a consumer it
cannot see, and an unseen consumer must never narrow a run.

Requirements:
- ✔︎ ✅ 🎯 A webui-only diff selects the frontend's consumers and `always`, not the suite.
- ✔︎ ✅ 🎯 A test reaching a mentioning source through imports, an imported module's parent
  `__init__`, its own ancestor package `__init__`, a `tests.` helper or a conftest is
  selected.
- ✔︎ ✅ 🎯 Any change set that is not webui-only selects everything.

Acceptance tests (tests/scripts/test_ci_seam_select.py):
- [if] a test imports a module that serves the frontend build [then] it is selected
  [⛔️ if the frontend could break it unseen].
- [if] a conftest mentions the frontend [then] every test beneath it is selected
  [⛔️ if a fixture-only consumer is dropped].
- [if] a test module's own ancestor `__init__.py` mentions the frontend [then] the test
  module is selected [⛔️ if collection runs that initializer unseen].
- [if] the diff touches python, infra or a queue draft [then] the selection is full
  [⛔️ if narrowing could reach a Python change].
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from scripts import ci_plan
from scripts.ci_plan_sources import PlanError, imported_modules, is_test_module, mentioned_strings
from scripts.ci_required_gate import merge_commit_changed_files
from scripts.ci_seams import plan_seams

FRONTEND_SCOPE = "webui_frontend"
NARROWABLE_SEAMS = frozenset({"webui"})
IMPORT_ROOTS = ("apps", "scripts", "ops", "tests")
DURATIONS_FILE = ".test_durations"


@dataclass(frozen=True)
class Selection:
    full: bool
    test_modules: frozenset[str]
    reason: str


# ----- the module graph ---------------------------------------------------------


def tracked_python_files(root: Path) -> list[str]:
    """Every tracked `.py` file, so a `.venv` or `node_modules` can never enter the graph."""
    listing = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--", "*.py"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return sorted(path for path in listing.split("\0") if path)


def module_name(path: str) -> str:
    return path[: -len(".py")].replace("/", ".").removesuffix(".__init__")


def _resolved_imports(imports: Iterable[str], known: Mapping[str, str]) -> set[str]:
    """Each import as the modules it RUNS: the longest known prefix (`a.b.c` may be a
    symbol in module `a.b`) plus every known parent package, whose `__init__` runs first."""
    resolved: set[str] = set()
    for name in imports:
        parts = name.split(".")
        resolved.update(".".join(parts[:i]) for i in range(1, len(parts) + 1) if ".".join(parts[:i]) in known)
    return resolved


def _own_ancestor_inits(path: str, tracked: frozenset[str]) -> set[str]:
    """Tracked package initializers pytest runs before importing this module (not itself).

    pytest runs a test package's initializer when it collects a module in it (Sol's P2 on
    #4677); importing `a.b.c` runs parent `__init__` files first, which `_resolved_imports`
    already covers for explicit imports."""
    deps: set[str] = set()
    for parent in PurePosixPath(path).parents:
        if parent == PurePosixPath("."):
            continue
        init = str(parent / "__init__.py")
        if init in tracked and init != path:
            deps.add(init)
    return deps


def _read(root: Path, path: str) -> str:
    try:
        return (root / path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise PlanError(f"cannot read {path}: {exc}") from None


@functools.cache
def frontend_consumers(root: Path, needles: tuple[str, ...]) -> frozenset[str]:
    """Paths of the tracked Python files the frontend can reach: the seeds that mention it,
    every importer of a seed (transitively), and every module beneath a reached conftest.
    Cached per tree, so a replay reads the tree once."""
    files = tracked_python_files(root)
    tracked = frozenset(files)
    known = {module_name(path): path for path in files}
    texts = {path: _read(root, path) for path in files}
    reached = {
        path
        for path, text in texts.items()
        if any(needle in literal for literal in mentioned_strings(text, path) for needle in needles)
    }
    if not reached:
        raise PlanError(f"no tracked Python file mentions {list(needles)}; the reader is not measuring")
    imports = {
        path: {known[name] for name in _resolved_imports(imported_modules(text, path, IMPORT_ROOTS), known)}
        | _own_ancestor_inits(path, tracked)
        for path, text in texts.items()
    }
    changed = True
    while changed:
        changed = False
        for path, deps in imports.items():
            if path not in reached and deps & reached:
                reached.add(path)
                changed = True
        for conftest in [p for p in reached if p.rsplit("/", 1)[-1] == "conftest.py"]:
            scope = PurePosixPath(conftest).parent
            beneath = {p for p in files if scope in PurePosixPath(p).parents} - reached
            if beneath:
                reached |= beneath
                changed = True
    return frozenset(reached)


# ----- selection ------------------------------------------------------------------


def select_tests(
    root: Path,
    config: ci_plan.Config,
    event_name: str,
    head_ref: str,
    changed_files: Sequence[str],
) -> Selection:
    plan = plan_seams(event_name, head_ref, changed_files)
    if plan.full:
        return Selection(True, frozenset(), plan.reason)
    if not plan.seams:
        return Selection(True, frozenset(), "no code seam touched; ci.yml's scope job decides")
    if not plan.seams <= NARROWABLE_SEAMS:
        return Selection(True, frozenset(), f"seams {sorted(plan.seams)} are not narrowable yet")
    triggered = [path for path in changed_files if ci_plan.matches(path, config.full_triggers)]
    if triggered:
        return Selection(True, frozenset(), f"full trigger(s): {triggered[:10]}")
    frontend = next((scope for scope in config.scopes if scope.name == FRONTEND_SCOPE), None)
    if frontend is None or not frontend.mentions:
        raise PlanError(f"{FRONTEND_SCOPE} with mentions is missing from ci/test-scopes.yml")
    consumers = frontend_consumers(root, frontend.mentions)
    always = {
        path for path in tracked_python_files(root) if is_test_module(path) and ci_plan.matches(path, config.always)
    }
    tests = frozenset(path for path in consumers | always if is_test_module(path))
    return Selection(False, tests, f"webui-only: {len(tests)} test modules reach the frontend or are always run")


def selected_share(selection: Selection, durations: Mapping[str, float]) -> float:
    """Fraction of recorded pytest seconds the selection would run (1.0 when full)."""
    if selection.full:
        return 1.0
    total = sum(durations.values())
    if total <= 0:
        raise PlanError(f"{DURATIONS_FILE} records no time; the share is unmeasured")
    picked = sum(seconds for node, seconds in durations.items() if node.split("::", 1)[0] in selection.test_modules)
    return picked / total


# ----- CLI ------------------------------------------------------------------------


def _durations(root: Path) -> dict[str, float]:
    return json.loads(_read(root, DURATIONS_FILE))


def _run_shadow(root: Path) -> int:
    event_name = os.environ.get("GITHUB_EVENT_NAME")
    if not event_name:
        raise RuntimeError("$GITHUB_EVENT_NAME is unset; this runs inside a GitHub Actions job")
    changed = merge_commit_changed_files(root) if event_name == "pull_request" else []
    selection = select_tests(root, ci_plan.read_config(), event_name, os.environ.get("GITHUB_HEAD_REF", ""), changed)
    share = selected_share(selection, _durations(root))
    summary = {"full": selection.full, "test_modules": len(selection.test_modules), "share": round(share, 4)}
    print(f"[ci-seam-select] shadow (nothing is skipped) selection={json.dumps(summary)} -- {selection.reason}")
    for module in sorted(selection.test_modules):
        print(f"[ci-seam-select] selected {module}")
    return 0


def _run_replay(root: Path, corpus: Path) -> int:
    """Selection share for each PR in a `{number, files}` corpus, on TODAY's tree: an
    estimate of what a merged PR would have run, not a replay of its own tree."""
    config, durations = ci_plan.read_config(), _durations(root)
    rows = json.loads(corpus.read_text(encoding="utf-8"))
    narrowed = [
        (pr["number"], selected_share(selection, durations))
        for pr in rows
        if not (selection := select_tests(root, config, "pull_request", "af--replay", pr["files"])).full
    ]
    if not narrowed:
        print(f"UNKNOWN: none of {len(rows)} PRs narrows; no share to report")
        return 2
    shares = sorted(share for _, share in narrowed)
    total_min = sum(durations.values()) / 60
    median = shares[len(shares) // 2]
    print(f"narrowed: {len(narrowed)} of {len(rows)} PRs (webui-only, no full trigger)")
    print(f"median share: {median:.1%} = {median * total_min:.1f} of {total_min:.1f} pytest-min")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("shadow", help="log this run's pytest selection; skips nothing")
    replay = commands.add_parser("replay", help="selection share over a {number, files} corpus")
    replay.add_argument("corpus", type=Path)
    args = parser.parse_args(argv)
    root = Path.cwd()
    if args.command == "shadow":
        return _run_shadow(root)
    if args.command == "replay":
        return _run_replay(root, args.corpus)
    raise AssertionError(f"unhandled command {args.command!r}")


if __name__ == "__main__":
    sys.exit(main())
