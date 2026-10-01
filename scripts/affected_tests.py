"""Print the pytest modules a change reaches, via a static import graph.

Read-only. Was used by the affected-test canary in `ci.yml` (SMARTEST-CI rounds 1 and 2,
replaced by the fast tier in round 6a); kept as the static lower bound for the round 5
planner. The canary ran these tests FIRST as a
fast non-blocking signal while the full sharded lane runs everything anyway.

Since DEVOPS-20 (the maintainer, Thu 1 Oct 2026, ADR-NEW-pr-heads-run-affected-tests) the graph is
also the core of `scripts/ci_test_selection.py`, which DOES narrow what a pull request
head runs in the five pytest shards. That use is only sound because the Trunk merge queue
draft, every main push and every `ci:trunk-repair` PR still run the full suite on the
exact tree that lands, and because the selector widens this lower bound with named
references, conftest reach and an explicit coupling map before it narrows anything.
Everything said below about this bound alone still holds.

WHAT THIS IS NOT. This is a LOWER bound on what a change can affect. It sees static
`import` and `from ... import` statements and nothing else, so it cannot see a conftest
fixture pulling a module in, `importlib` and other dynamic imports, a subprocess boundary,
a plugin registered by entry point, or a dependency on a data file. A test it does not name
can still be broken by the change.

That is why the output may only ever be used to run tests EARLIER, never to skip them. The
moment anything deselects on this basis, the bound stops being conservative in the safe
direction, and the correct instrument for that is per-test coverage, not imports.

Measured on 10 merged PRs Wed 9 Sep 2026: the median change reached 1.8% of the 719 pytest
modules, the largest reached 15.7%, and two reached under 1%.

FAIL LOUD. A changed `.py` path the graph cannot resolve (deleted, renamed away, under a
skipped directory, or simply not there) exits 3 and names the paths. Until Wed 16 Sep 2026
it contributed nothing and exited 0, so an unreadable change and a change no test reaches
printed the same empty selection. Exit codes: 0 selection printed (possibly empty, with the
reason on stderr), 3 UNKNOWN.

Usage:
    python -m scripts.affected_tests --base origin/main
    python -m scripts.affected_tests --files apps/webui/server/routes/ingest.py
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
EXIT_UNKNOWN = 3

#: Directories that hold no importable first-party source.
SKIP_PARTS = frozenset({".venv", "node_modules", ".git", "build", "dist", ".tmp"})


def _module_name(relative: Path) -> str | None:
    """Dotted module name for a repo-relative path, or None when not importable."""
    if relative.suffix != ".py":
        return None
    parts = list(relative.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else None


def _imports_of(path: Path) -> set[str]:
    """Every dotted name this file imports, including `from x import y` as `x.y`.

    A file that does not parse contributes nothing rather than aborting the graph: a
    syntax error is the test suite's problem to report, not this script's.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
    except SyntaxError:
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = list(path.relative_to(REPO).parent.parts)
                if node.level > 1:
                    base = base[: len(base) - (node.level - 1)]
                prefix = ".".join(base)
                found.add(f"{prefix}.{node.module}" if node.module else prefix)
            elif node.module:
                found.add(node.module)
                found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


class UnresolvedPaths(Exception):
    """Changed Python paths that are not modules in the graph, so their reach is unknown."""

    def __init__(self, paths: list[str]) -> None:
        super().__init__(", ".join(paths))
        self.paths = paths


def _source_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.py") if not SKIP_PARTS & set(p.parts)]


def build_graph(root: Path = REPO) -> tuple[dict[str, set[str]], dict[str, Path]]:
    """Return (module -> modules that import it, module -> repo-relative path)."""
    module_of: dict[str, Path] = {}
    files = _source_files(root)
    for path in files:
        relative = path.relative_to(root)
        name = _module_name(relative)
        if name:
            module_of[name] = relative
    importers: dict[str, set[str]] = defaultdict(set)
    for path in files:
        me = _module_name(path.relative_to(root))
        if not me:
            continue
        for target in _imports_of(path):
            parts = target.split(".")
            # An attribute import resolves to the longest prefix that is a real module.
            for cut in range(len(parts), 0, -1):
                candidate = ".".join(parts[:cut])
                if candidate in module_of:
                    importers[candidate].add(me)
                    break
    return importers, module_of


def _is_test(relative: Path) -> bool:
    return relative.name.startswith("test_") and relative.suffix == ".py"


def affected_tests(changed: list[str], root: Path = REPO) -> list[str]:
    """Test modules reachable from the changed files, plus changed tests themselves.

    Raises UnresolvedPaths for a changed `.py` path that is not a module in the graph: a
    deleted module's importers are exactly the tests most likely to break, and the graph
    built from the current tree cannot name them.
    """
    importers, module_of = build_graph(root)
    changed_set = {str(Path(c)) for c in changed if c.endswith(".py")}
    known_paths = {str(relative) for relative in module_of.values()}
    unresolved = sorted(changed_set - known_paths)
    if unresolved:
        raise UnresolvedPaths(unresolved)
    seeds = {name for name, relative in module_of.items() if str(relative) in changed_set}
    seen = reverse_reach(seeds, importers)
    return sorted(str(module_of[name]) for name in seen if _is_test(module_of[name]))


def reverse_reach(seeds: set[str], importers: dict[str, set[str]]) -> set[str]:
    """The seed modules plus every module that imports one of them, transitively."""
    seen = set(seeds)
    stack = list(seeds)
    while stack:
        for importer in importers.get(stack.pop(), ()):
            if importer not in seen:
                seen.add(importer)
                stack.append(importer)
    return seen


def _changed_against(base: str, root: Path = REPO) -> list[str]:
    """Files changed against `base`, via the merge base so a stale branch is not noise.

    `--no-renames` so a rename lists its OLD path too: rename detection reports only the new
    one, and the module that vanished is the one whose importers break.
    """
    merge_base = subprocess.run(
        ["git", "merge-base", "HEAD", base],
        capture_output=True,
        text=True,
        cwd=root,
        check=False,
    )
    reference = merge_base.stdout.strip() if merge_base.returncode == 0 else base
    diff = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", reference, "HEAD"],
        capture_output=True,
        text=True,
        cwd=root,
        check=True,
    )
    return [line for line in diff.stdout.splitlines() if line.endswith(".py")]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--base", help="git ref to diff against, e.g. origin/main")
    group.add_argument("--files", nargs="+", help="repo-relative paths, instead of a diff")
    arguments = parser.parse_args(argv)

    changed = arguments.files if arguments.files else _changed_against(arguments.base)
    python_changed = [path for path in changed if path.endswith(".py")]
    if not python_changed:
        print("# no Python file changed", file=sys.stderr)
        return 0
    try:
        selection = affected_tests(python_changed)
    except UnresolvedPaths as unresolved:
        print(
            f"UNKNOWN: {len(unresolved.paths)} changed Python path(s) are not modules in the "
            f"import graph (deleted, renamed, skipped or missing), so their reach cannot be "
            f"computed: {', '.join(unresolved.paths)}",
            file=sys.stderr,
        )
        return EXIT_UNKNOWN
    if not selection:
        print(
            f"# {len(python_changed)} Python file(s) changed; no test module reaches them",
            file=sys.stderr,
        )
    for path in selection:
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
