"""Merge per-process traces from `scripts.impact_trace_plugin` into one impact map, and select.

SMARTEST-CI (specs/ci-fail-fast.md, part 3). Pure except for the CLI at the bottom.

The map is keyed by test module. `select_modules` answers "which test modules could a change
to these paths affect" from what the modules were OBSERVED to touch:

    a file the module (or a fixture it uses, or a conftest above it) opened
    a directory it listed, if the changed path is DIRECTLY in it. `os.scandir` observes
    only a directory's own entries, so a test that walks a tree scans every level it
    descends into and each one is recorded; crediting a listing with the whole subtree
    instead makes one startup scan of `apps/` select every module for every change
    a glob it expanded, if the changed path matches the pattern

It returns UNKNOWN (never an empty selection) for what tracing cannot see: a module that
spawned a subprocess is selected for every change, because its child's reads are untraced.

Usage:
    python -m scripts.impact_map merge .tmp/impact --out .tmp/impact-map.json
    python -m scripts.impact_map select .tmp/impact-map.json apps/webui/frontend/src/x.ts
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import sys
import tomllib
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

MODULE = "module:"
FIXTURE = "fixture:"
DIRECTORY = "dir:"

REPO = Path(__file__).resolve().parent.parent
PYPROJECT = REPO / "pyproject.toml"


def _test_roots(pyproject: Path = PYPROJECT) -> tuple[str, ...]:
    """The directories pytest is configured to collect from, read from `pyproject.toml`.

    Read rather than hardcoded so the selector cannot drift from the collector: a new test
    root added to `testpaths` is one this selector starts honoring in the same commit. A
    missing or empty `testpaths` RAISES, because guessing `tests` would silently reinstate
    the assumption this function exists to remove.
    """
    with pyproject.open("rb") as handle:
        roots = tomllib.load(handle).get("tool", {}).get("pytest", {}).get(
            "ini_options", {}
        ).get("testpaths")
    if not roots:
        raise SystemExit(f"UNKNOWN: {pyproject} sets no [tool.pytest.ini_options] testpaths")
    return tuple(str(PurePosixPath(root)) for root in roots)


TEST_ROOTS = _test_roots()


@dataclass(frozen=True)
class Touches:
    files: frozenset[str]
    dirs: frozenset[str]
    globs: frozenset[str]

    def reaches(self, path: str) -> bool:
        if path in self.files:
            return True
        if str(PurePosixPath(path).parent) in self.dirs:
            return True
        return any(_glob_matches(pattern, path) for pattern in self.globs)


@dataclass(frozen=True)
class ModuleImpact:
    touches: Touches
    fixtures: frozenset[str]
    spawns: frozenset[str]


@dataclass(frozen=True)
class ImpactMap:
    modules: dict[str, ModuleImpact]
    fixtures: dict[str, Touches]
    directories: dict[str, Touches]


def _glob_matches(pattern: str, path: str) -> bool:
    """Both sides are repo-relative (the tracer normalizes patterns), so this is `fnmatch`.

    `fnmatch` lets `*` cross a path separator, so `apps/*.ts` also matches `apps/x/y.ts`.
    That over-selects rather than under-selects, which is the direction selection is allowed
    to err in.
    """
    return fnmatch.fnmatch(path, pattern)


# ----- merge -----


def merge_traces(payloads: list[dict]) -> dict:
    """Union every process's records. Keys are stable across processes."""
    merged: dict[str, dict] = defaultdict(
        lambda: {"files": set(), "dirs": set(), "globs": set(), "fixtures": set(), "spawns": set()}
    )
    for payload in payloads:
        for key, record in payload["records"].items():
            target = merged[key]
            for field in ("files", "dirs", "globs", "fixtures", "spawns"):
                target[field].update(record[field])
    return {
        key: {
            field: sorted(value[field])
            for field in ("files", "dirs", "globs", "fixtures", "spawns")
        }
        for key, value in sorted(merged.items())
    }


def load_map(records: dict) -> ImpactMap:
    def touches(record: dict) -> Touches:
        return Touches(
            frozenset(record["files"]), frozenset(record["dirs"]), frozenset(record["globs"])
        )

    modules, fixtures, directories = {}, {}, {}
    for key, record in records.items():
        if key.startswith(MODULE):
            modules[key[len(MODULE) :]] = ModuleImpact(
                touches(record), frozenset(record["fixtures"]), frozenset(record["spawns"])
            )
        elif key.startswith(FIXTURE):
            fixtures[key[len(FIXTURE) :]] = touches(record)
        elif key.startswith(DIRECTORY):
            directories[key[len(DIRECTORY) :]] = touches(record)
    return ImpactMap(modules, fixtures, directories)


# ----- select -----


def select_modules(
    impact: ImpactMap,
    changed: list[str],
    exists: Callable[[str], bool] = lambda path: (REPO / path).exists(),
) -> frozenset[str]:
    """Test modules the observed traces say a change to `changed` can reach.

    A changed test module the map has never seen is selected too. The map can only speak
    about what it observed, so a test added since it was built is UNKNOWN, and a pull
    request that adds a test must run it.

    A DELETED one is not, and the check is on the EMITTED set rather than on one branch of
    it. pytest handed a path that no longer exists errors out instead of running anything, so
    ANY emitted path that is not on disk fails the job, however it was selected. A diff lists
    a removed file exactly like an added one, so a pull request whose only change is deleting
    a test would fail CI on the file it deleted; and the map is built from an older commit,
    so a module deleted since is still in `impact.modules` and would be emitted even when the
    diff never mentions it. `exists` is a seam so the rule can be tested without a filesystem.
    """
    selected: set[str] = {
        path for path in changed if _is_test_module(path) and path not in impact.modules
    }
    fixtures_hit = {
        name for name, touches in impact.fixtures.items() if any(map(touches.reaches, changed))
    }
    hit_dirs = [
        directory
        for directory, touches in impact.directories.items()
        if any(map(touches.reaches, changed))
    ]
    for module, entry in impact.modules.items():
        if (
            entry.spawns
            or module in changed
            or any(map(entry.touches.reaches, changed))
            or entry.fixtures & fixtures_hit
            or any(_is_under(module, directory) for directory in hit_dirs)
        ):
            selected.add(module)
    return frozenset(path for path in selected if exists(path))


def _is_test_module(path: str) -> bool:
    """A file pytest would actually collect: inside a configured test root AND named the way
    pytest names tests.

    The naming half alone is not enough. `scripts/bench/q0-offline/q0_leak_test.py` matches
    the pattern and is NOT collected, because `pyproject.toml` sets `testpaths = ["tests"]`.
    Emitted as a test to run, pytest imports its top-level benchmark code and the job fails
    rather than running a test, so a selector that widened to be safe would break the build.
    """
    if not path.endswith(".py"):
        return False
    if not any(_is_under(path, root) for root in TEST_ROOTS):
        return False
    name = PurePosixPath(path).name
    return name.startswith("test_") or name.endswith("_test.py")


def _is_under(module: str, directory: str) -> bool:
    return directory in (".", "") or module.startswith(directory.rstrip("/") + "/")


# ----- CLI -----


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    merge = sub.add_parser("merge")
    merge.add_argument("trace_dir", type=Path)
    merge.add_argument("--out", type=Path, required=True)
    select = sub.add_parser("select")
    select.add_argument("map", type=Path)
    select.add_argument("paths", nargs="+")
    args = parser.parse_args(argv)

    if args.command == "merge":
        traces = sorted(args.trace_dir.glob("impact-*.json"))
        if not traces:
            print(f"UNKNOWN: no impact-*.json under {args.trace_dir}", file=sys.stderr)
            return 3
        merged = merge_traces([json.loads(path.read_text(encoding="utf-8")) for path in traces])
        args.out.write_text(json.dumps(merged, indent=1), encoding="utf-8")
        modules = sum(key.startswith(MODULE) for key in merged)
        spawning = sum(
            bool(key.startswith(MODULE) and value["spawns"]) for key, value in merged.items()
        )
        print(
            f"merged {len(traces)} trace(s): {modules} test modules, {spawning} spawn subprocesses"
        )
        return 0
    impact = load_map(json.loads(args.map.read_text(encoding="utf-8")))
    for module in sorted(select_modules(impact, args.paths)):
        print(module)
    return 0


if __name__ == "__main__":
    sys.exit(main())
