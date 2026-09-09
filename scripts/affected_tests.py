"""Print the pytest modules a change reaches, via a static import graph.

Read-only. Used by the affected-test canary in `ci.yml`, which runs these tests FIRST as a
fast non-blocking signal while the full sharded lane runs everything anyway.

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
    """Test modules reachable from the changed files, plus changed tests themselves."""
    importers, module_of = build_graph(root)
    changed_set = {str(Path(c)) for c in changed}
    seeds = {name for name, relative in module_of.items() if str(relative) in changed_set}
    seen = set(seeds)
    stack = list(seeds)
    while stack:
        for importer in importers.get(stack.pop(), ()):
            if importer not in seen:
                seen.add(importer)
                stack.append(importer)
    return sorted(str(module_of[name]) for name in seen if _is_test(module_of[name]))


def _changed_against(base: str) -> list[str]:
    """Files changed against `base`, via the merge base so a stale branch is not noise."""
    merge_base = subprocess.run(
        ["git", "merge-base", "HEAD", base], capture_output=True, text=True, cwd=REPO
    )
    reference = merge_base.stdout.strip() if merge_base.returncode == 0 else base
    diff = subprocess.run(
        ["git", "diff", "--name-only", reference, "HEAD"],
        capture_output=True,
        text=True,
        cwd=REPO,
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
    if not changed:
        print("# no Python file changed", file=sys.stderr)
        return 0
    for path in affected_tests(changed):
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
