"""Pick the frontend unit tests (`tests/unit/*.test.mjs`) a pull request's change can reach.

ci.yml's `frontend` job ran all ~1,180 unit test files on every pull request it ran on (median
7.7 of its 10.8 minutes, Mon 9 Nov 2026). This narrows a pull request's run the way the pytest
selector does: build a file graph over the frontend tree, walk it backwards from the changed
files, and keep the unit test files it reaches. Pushes, the schedule and dispatches run them all.

Graph edges, from a file to what it reads:
  - import / export-from / dynamic import specifiers that are relative or `$lib/`;
  - in `*.test.mjs` files only (helpers carry generic roots), any string literal shaped like a
    frontend path (`./`, `../`, `src/`, `$lib/`), resolved against the file's directory and the
    frontend root; a literal naming a src/ directory of at most MAX_DIRECTORY_FILES files reads
    every file under it (a larger one is a whole-tree guard, left to the push to main).

Falls back to ALL (fail-safe, never fewer) when a global input changed (manifest, lockfile,
tsconfig, svelte/vite config, the unit-test runner config) or the selection is over
`ALL_SHARE` of the suite. Reaching nothing is `none`: the job's other steps still run.

Requirements:
- ✔︎ ✅ 🎯 A changed source file selects every unit test that reaches it, directly or transitively.
    [if] a test loads src/lib/a.ts, which imports ./b.ts, and b.ts changes [then ⛔️ unless selected]
    [if] a test names a directory and a file under it changes [then ⛔️ unless selected]
    [if] a changed test file is not selected [then ⛔️]
- ✔︎ ✅ 🎯 A hub selects its direct tests and a capped sample, never its whole reach.
    [if] a hub change selects more than its direct tests plus HUB_SAMPLE [then ⛔️]
    [if] a hub's direct test is not selected [then ⛔️]
- ✔︎ ✅ 🎯 Whole-tree guards always run; backend changes select the backend-booting tests.
    [if] a listed always-run test is missing from a narrowed selection [then ⛔️]
    [if] a .py change does not select a test that starts the real daemon [then ⛔️]
- ✔︎ ✅ 🎯 A computed-path loader is selected through its explicit link.
    [if] a change matching a LINKS_FILE glob does not select the linked test [then ⛔️]
- ✔︎ ✅ 🎯 Global inputs and oversize selections run everything.
    [if] package.json or pnpm-lock.yaml changes and the mode is not `all` [then ⛔️]
    [if] more than ALL_SHARE of the suite is selected and the mode is not `all` [then ⛔️]
    [if] an unrelated test is selected for a leaf change [then ⛔️]

-Claude
"""

from __future__ import annotations

import argparse
import fnmatch
import functools
import hashlib
import os
import re
import sys
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path

from scripts.ci_required_gate import merge_commit_changed_files

FRONTEND = "apps/webui/frontend"
UNIT_DIR = "tests/unit"
SOURCE_SUFFIXES = (".ts", ".tsx", ".js", ".mjs", ".svelte", ".json", ".css")
#: Inputs every unit test depends on through the toolchain; a change here runs the suite.
GLOBAL_INPUTS = re.compile(
    r"^(package\.json|pnpm-lock\.yaml|pnpm-workspace\.yaml|tsconfig[^/]*\.json|svelte\.config\.js"
    r"|vite[^/]*\.ts|patches/.*|tests/unit/esbuild-tsconfig\.mjs|tests/unit/load-typescript\.mjs"
    r"|tests/unit/import-bundled-source\.mjs)$"
)
#: A test naming a src/ directory reads it all; a directory larger than this is a whole-tree guard
#: (it would select itself for every change), which the push to main runs instead.
MAX_DIRECTORY_FILES = 60
#: A module read by at least this many non-test frontend files is a hub (src/lib/api/client.ts,
#: base.ts, errors.ts, api-types.ts and the rb/*-types modules, Mon 9 Nov 2026): every change to
#: it reached about 765 of 1,229 unit test files. A hub selects its direct tests plus HUB_SAMPLE
#: of the rest, not its whole reach; the push to main runs the rest.
HUB_SOURCE_READERS = 40
HUB_SAMPLE = 25
#: Whole-tree guard tests every narrowed run keeps (repo-relative list, one frontend test per line).
ALWAYS_RUN_FILE = "ci/frontend-unit-always-run.txt"
#: Explicit edges for tests that load code through a computed path (`<glob> -> <test>`).
LINKS_FILE = "ci/frontend-unit-links.txt"
#: A unit test that boots the Python backend (the real daemon, a fixture server, a uv-run script)
#: depends on backend code no import edge reaches; any backend change selects all of them.
BACKEND_TEST = re.compile(r"fixtures/real-daemon\.mjs|fixture-process\.mjs|resolveUv\(|\.py['\"]")
BACKEND_CHANGE = re.compile(r"\.py$|^apps/webui/openapi\.json$|^uv\.lock$|^pyproject\.toml$|^pylock[^/]*\.toml$")
#: Over this share of the suite, the whole suite is cheaper to reason about than the list.
ALL_SHARE = 0.5

_IMPORT = re.compile(r"""(?:\bfrom\s*|\bimport\s*\(\s*|\bimport\s+)['"]([^'"]+)['"]""")
_PATH_LITERAL = re.compile(r"""['"`]((?:\.{1,2}/|src/|tests/|\$lib/)[^'"`\s$]*)['"`]""")


@dataclass(frozen=True)
class Selection:
    mode: str  # all | selected | none
    reason: str
    tests: tuple[str, ...]


# ----- graph -----------------------------------------------------------------------------


@functools.cache
def _resolve(root: Path, base: Path, spec: str) -> tuple[Path, ...]:
    """Frontend files `spec` names from `base`: a file, an extension variant, or a small src/ directory's files."""
    if spec.startswith("$lib/"):
        candidates = [root / "src/lib" / spec[5:]]
    elif spec.startswith(("src/", "tests/")):
        candidates = [root / spec, base / spec]
    else:
        candidates = [base / spec, root / spec]
    for candidate in candidates:
        path = Path(os.path.normpath(candidate))
        if path.is_dir():
            if not path.is_relative_to(root / "src"):
                return ()
            files = tuple(p for p in path.rglob("*") if p.is_file() and p.suffix in SOURCE_SUFFIXES)
            return files if len(files) <= MAX_DIRECTORY_FILES else ()
        for variant in (path, *(Path(f"{path}{s}") for s in SOURCE_SUFFIXES), path / "index.ts", path / "index.js"):
            if variant.is_file():
                return (variant,)
    return ()


def _reads(root: Path, path: Path) -> set[Path]:
    text = path.read_text(encoding="utf-8", errors="replace")
    specs = {m for m in _IMPORT.findall(text) if m.startswith((".", "$lib/"))}
    if path.name.endswith(".test.mjs"):
        specs |= set(_PATH_LITERAL.findall(text))
    return {target for spec in specs for target in _resolve(root, path.parent, spec)}


def reverse_graph(root: Path) -> dict[Path, set[Path]]:
    """file -> the files that read it, over src/ and tests/unit/."""
    readers: dict[Path, set[Path]] = defaultdict(set)
    files = [p for d in ("src", UNIT_DIR) for p in (root / d).rglob("*") if p.is_file() and p.suffix in SOURCE_SUFFIXES]
    for path in files:
        if path.suffix in (".json", ".css"):
            continue
        for target in _reads(root, path):
            readers[target.resolve()].add(path.resolve())
    return readers


# ----- selection -------------------------------------------------------------------------


def _is_unit_test(root: Path, path: Path) -> bool:
    return path.parent == (root / UNIT_DIR).resolve() and path.name.endswith(".test.mjs")


def _is_hub(readers: dict[Path, set[Path]], node: Path) -> bool:
    return sum(1 for r in readers.get(node, ()) if not r.name.endswith(".test.mjs")) >= HUB_SOURCE_READERS


def _walk(readers: dict[Path, set[Path]], start: list[Path], *, stop_at_hubs: bool) -> tuple[set[Path], set[Path]]:
    """Every file reading `start` transitively; with `stop_at_hubs`, a hub's readers are not followed."""
    seen: set[Path] = set()
    hubs: set[Path] = set()
    queue = deque(start)
    while queue:
        node = queue.popleft()
        if node in seen:
            continue
        seen.add(node)
        if stop_at_hubs and _is_hub(readers, node):
            hubs.add(node)
            continue
        queue.extend(readers.get(node, ()))
    return seen, hubs


def _hub_tests(root: Path, readers: dict[Path, set[Path]], hub: Path) -> set[Path]:
    """A hub's direct tests plus a fixed, deterministic sample of HUB_SAMPLE tests from its full reach."""
    direct = {r for r in readers.get(hub, ()) if _is_unit_test(root, r)}
    reach, _ = _walk(readers, [hub], stop_at_hubs=False)
    rest = sorted(
        (r for r in reach if _is_unit_test(root, r) and r not in direct),
        key=lambda p: hashlib.sha1(p.name.encode()).hexdigest(),
    )
    return direct | set(rest[:HUB_SAMPLE])


def always_run_tests(root: Path) -> set[Path]:
    """The guard tests `ALWAYS_RUN_FILE` names; a missing file or a listed test that does not exist fails."""
    listing = root.parents[2] / ALWAYS_RUN_FILE
    names = [line.split("#", 1)[0].strip() for line in listing.read_text(encoding="utf-8").splitlines()]
    tests = {(root / name).resolve() for name in names if name}
    missing = sorted(str(t) for t in tests if not t.is_file())
    if missing:
        raise FileNotFoundError(f"{ALWAYS_RUN_FILE} names tests that do not exist: {missing}")
    return tests


def linked_tests(root: Path, relative: list[str]) -> set[Path]:
    """Tests `LINKS_FILE` ties to a changed frontend path; a listed test that does not exist fails."""
    tests: set[Path] = set()
    for line in (root.parents[2] / LINKS_FILE).read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        pattern, _, test = (part.strip() for part in line.partition("->"))
        path = (root / test).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"{LINKS_FILE} links {pattern} to a test that does not exist: {test}")
        if any(fnmatch.fnmatchcase(changed, pattern) for changed in relative):
            tests.add(path)
    return tests


def backend_tests(root: Path) -> set[Path]:
    return {
        p.resolve()
        for p in (root / UNIT_DIR).glob("*.test.mjs")
        if BACKEND_TEST.search(p.read_text(encoding="utf-8", errors="replace"))
    }


def select(root: Path, changed: list[str]) -> Selection:
    root = root.resolve()
    prefix = f"{FRONTEND}/"
    relative = [c[len(prefix) :] for c in changed if c.startswith(prefix)]
    global_hits = [c for c in relative if GLOBAL_INPUTS.match(c)]
    if global_hits:
        return Selection("all", f"global input changed: {', '.join(global_hits[:5])}", ())
    suite = sorted(p.resolve() for p in (root / UNIT_DIR).glob("*.test.mjs"))
    readers = reverse_graph(root)
    seen, hubs = _walk(readers, [(root / c).resolve() for c in relative], stop_at_hubs=True)
    for hub in hubs:
        seen |= _hub_tests(root, readers, hub)
    if any(BACKEND_CHANGE.search(c) for c in changed):
        seen |= backend_tests(root)
    seen |= always_run_tests(root) | linked_tests(root, relative)
    tests = sorted(p for p in seen if _is_unit_test(root, p) and p.exists())
    if len(tests) > ALL_SHARE * len(suite):
        return Selection("all", f"{len(tests)} of {len(suite)} unit test files reached (over {ALL_SHARE:.0%})", ())
    names = tuple(str(p.relative_to(root)) for p in tests)
    if not names:
        return Selection("none", f"no unit test reaches the {len(relative)} changed frontend file(s)", ())
    return Selection("selected", f"{len(names)} of {len(suite)} unit test files reach the change", names)


# ----- CLI -------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--changed-from-merge-commit", action="store_true", help="diff HEAD^1..HEAD (pull_request)")
    parser.add_argument("--changed", nargs="*", default=None, help="changed repo paths (instead of git)")
    parser.add_argument("--list-out", type=Path, required=True, help="file to write the selected test paths into")
    args = parser.parse_args(argv)
    if args.changed_from_merge_commit:
        changed = merge_commit_changed_files(args.repo)
    elif args.changed is not None:
        changed = args.changed
    else:
        parser.error("give --changed-from-merge-commit or --changed")
    result = select(args.repo / FRONTEND, changed)
    args.list_out.write_text("".join(f"{t}\n" for t in result.tests), encoding="utf-8")
    print(f"[frontend-unit-selection] mode={result.mode} -- {result.reason}")
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"mode={result.mode}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
