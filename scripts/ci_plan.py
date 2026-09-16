"""Turn a pull request's diff into a pytest plan: SKIP_PYTEST, SCOPED or FULL.

SMARTEST-CI round 5 (`specs/ci-fail-fast.md`). This REPORTS a plan; nothing acts on it.
Selection becomes a gate only once the miss audit has produced a recall number, which it has
not, so the only safe way to be wrong here is to over-select, and every rule below that
cannot decide answers FULL.

Scope names are the module names Mergify's `mergify ci scopes` will use, so queue batching
by scope needs no renames later.

What the verdicts mean:

    SKIP_PYTEST  no changed path touches any scope, any always-run test, or any FULL
                 trigger. Documentation and workflow-free config changes land here.
    SCOPED       the union of every touched scope's tests, plus the always-run tests that
                 no scope claims.
    FULL         a FULL trigger, a deleted or renamed `.py`, a changed source path no scope
                 claims, or a selection so large that narrowing buys nothing.

An EMPTY change list is none of these. A diff that read nothing and a diff of nothing look
identical from here, and `scripts/affected_tests.py` shipped that exact hole (0 modules,
exit 0, a canary green 25 runs of 28 without running a test). `plan` raises instead.
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO / "ci" / "test-scopes.yml"
CAP_FRACTION = 0.6


class PlanError(Exception):
    """The plan could not be MEASURED. Never rendered as a verdict."""


class Verdict(StrEnum):
    SKIP_PYTEST = "SKIP_PYTEST"
    SCOPED = "SCOPED"
    FULL = "FULL"


@dataclass(frozen=True)
class Change:
    """One line of `git diff --name-status`: the status letter and the path."""

    status: str
    path: str


@dataclass(frozen=True)
class Scope:
    name: str
    sources: tuple[str, ...]
    tests: tuple[str, ...]
    # Scopes whose TESTS import this scope's sources, so a change here can break them.
    # DERIVED, never hand-written: `observed_dependents` recomputes it from the imports and
    # `tests/scripts/test_ci_plan.py` fails when the two disagree, so a new import across a
    # scope boundary forces this file to move rather than silently narrowing the selection.
    dependents: tuple[str, ...] = ()


@dataclass(frozen=True)
class Config:
    full_triggers: tuple[str, ...]
    always: tuple[str, ...]
    scopes: tuple[Scope, ...]


@dataclass(frozen=True)
class Plan:
    verdict: Verdict
    scopes: tuple[str, ...]
    test_paths: tuple[str, ...]
    reason: str


# ----- pure: pattern matching -----


def _regex(pattern: str) -> re.Pattern[str]:
    """A glob as a regex. `**` crosses directories, `*` does not, a trailing `/` is a prefix.

    Written out rather than delegated to `fnmatch`, whose `*` crosses `/` and would make
    `apps/*/cli.py` match `apps/a/b/cli.py`.
    """
    if pattern.endswith("/"):
        return re.compile(re.escape(pattern) + ".*")
    table = {"**/": "(?:.*/)?", "**": ".*", "*": "[^/]*", "?": "[^/]"}
    parts = re.split(r"(\*\*/|\*\*|\*|\?)", pattern)
    return re.compile("".join(table.get(part, re.escape(part)) for part in parts) + "$")


def matches(path: str, patterns: tuple[str, ...]) -> bool:
    return any(_regex(pattern).match(path) for pattern in patterns)


# ----- pure: the plan -----

# INVERTED, deliberately. This was a whitelist of source suffixes, so anything under a source
# root wearing an extension nobody had thought of -- `.js`, `.tsx`, `.json`, `.yaml` -- was not
# source, matched no scope, and planned SKIP_PYTEST. Sol's P1 on #3339. A whitelist of what is
# source has to be completed before a new language is safe; a whitelist of what is HARMLESS has
# to be completed before a new language is SKIPPED, and the unlisted case fails closed.
_HARMLESS_SUFFIXES = (".md", ".rst", ".txt", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico")
_SOURCE_ROOTS = ("apps/", "scripts/", "ops/", "tests/")


def _is_source(path: str) -> bool:
    return path.startswith(_SOURCE_ROOTS) and not path.endswith(_HARMLESS_SUFFIXES)


def _with_dependents(hit: set[str], by_name: dict[str, Scope]) -> set[str]:
    """`hit` plus the scopes each hit scope lists as depending on it. ONE HOP, deliberately.

    `dependents` is already transitive, because `observed_dependents` closes over the SOURCE
    import graph before it asks which tests import what. Walking these edges transitively as
    well is not just redundant, it is WRONG: the edge says "this scope's tests import that
    scope's sources", and that relation does not compose. Measured on this tree, chaining it
    put every one of 60 sampled merges into FULL -- a 61% SCOPED rate to zero -- because one
    edge into a widely-imported scope reached the whole repository in two hops.
    """
    out = set(hit)
    for name in hit:
        if name in by_name:
            out.update(by_name[name].dependents)
    return out


def plan(
    changes: tuple[Change, ...], config: Config, cap_fraction: float = CAP_FRACTION
) -> Plan:
    """The plan for one diff. Pure: no filesystem, no git, no network."""
    if not changes:
        raise PlanError(
            "the change list is empty, which a diff of nothing and a diff that failed to "
            "read both produce; refusing to report a verdict"
        )
    if not config.scopes:
        raise PlanError("the scope config declares no scopes")

    removed = [c.path for c in changes if c.status[:1] in ("D", "R") and c.path.endswith(".py")]
    if removed:
        return Plan(
            Verdict.FULL,
            (),
            (),
            f"a .py file was deleted or renamed: {removed[0]}",
        )

    paths = [c.path for c in changes]
    triggered = [p for p in paths if matches(p, config.full_triggers)]
    if triggered:
        return Plan(Verdict.FULL, (), (), f"full trigger: {triggered[0]}")

    by_name = {s.name: s for s in config.scopes}
    hit: set[str] = set()
    unclaimed_source: list[str] = []
    for path in paths:
        owners = [s.name for s in config.scopes if matches(path, s.sources + s.tests)]
        if owners:
            hit.update(owners)
        elif matches(path, config.always):
            hit.add(_ALWAYS)
        elif _is_source(path):
            unclaimed_source.append(path)
    if unclaimed_source:
        return Plan(
            Verdict.FULL,
            (),
            (),
            f"a source path no scope claims: {unclaimed_source[0]}",
        )
    if not hit:
        return Plan(Verdict.SKIP_PYTEST, (), (), "no changed path touches a scope")

    # THE CLOSURE. Selecting only the scope that OWNS a changed path leaves every suite that
    # imports it unrun while the plan claims they were out of selection, and under-selection
    # is the direction this planner is not allowed to be wrong in. Applied BEFORE the cap, so
    # a change that pulls in most of the tree answers FULL rather than a wide SCOPED.
    hit = _with_dependents(hit, by_name)
    named = tuple(sorted(hit - {_ALWAYS}))
    if len(named) > max(1, int(len(config.scopes) * cap_fraction)):
        return Plan(
            Verdict.FULL,
            named,
            (),
            f"{len(named)} of {len(config.scopes)} scopes selected, over the cap",
        )
    tests = {t for s in config.scopes if s.name in named for t in s.tests}
    return Plan(
        Verdict.SCOPED,
        named,
        tuple(sorted(tests | set(config.always))),
        f"{len(named)} scope(s) touched",
    )


_ALWAYS = "<always>"


# ----- I/O: config and git -----


def read_config(path: Path = CONFIG_PATH) -> Config:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PlanError(f"cannot read {path}: {exc}") from None
    if not isinstance(raw, dict):
        raise PlanError(f"{path} is not a mapping: {raw!r}")
    scopes = raw.get("scopes")
    if not isinstance(scopes, list) or not scopes:
        raise PlanError(f"{path} declares no scopes")
    return Config(
        tuple(raw.get("full_triggers") or ()),
        tuple(raw.get("always") or ()),
        tuple(
            Scope(
                entry["name"],
                tuple(entry["sources"]),
                tuple(entry["tests"]),
                tuple(entry.get("dependents") or ()),
            )
            for entry in scopes
        ),
    )


_TOP_PACKAGES = ("apps", "scripts", "ops")


def is_test_module(path: str) -> bool:
    """pytest's own two shapes. Discovery and the completeness invariant have to agree on
    this: they used different predicates, so a `foo_test.py` could satisfy the ownership
    check while never being scanned for the imports that put its suite in a scoped plan.
    Sol's P1 on #3339."""
    name = path.rsplit("/", 1)[-1]
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def test_modules(root: Path) -> list[Path]:
    """Every test module under `root`, by the one predicate above."""
    return sorted(p for p in root.rglob("*.py") if is_test_module(p.as_posix()))


def imported_packages(text: str, where: str) -> set[str]:
    """The `apps.x` / `scripts.x` / `ops.x` packages a module imports, read with the AST.

    A regular expression missed `from apps import engine_core` outright and saw only the
    first name in `import apps.foo, apps.bar`. Sol's P1 on #3339, and the reason it was
    invisible: the generated `dependents` and the guard that checks it used the SAME matcher,
    so the guard agreed with a derivation that was wrong in exactly the same way. An equality
    between two runs of one broken instrument proves nothing.

    A file that does not parse RAISES rather than returning an empty set: no imports found
    and could not look are the same value, and the second one must never narrow a plan.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        raise PlanError(f"cannot parse {where}: {exc}") from None
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            # `from apps import engine_core` names the package in the ALIAS, not the module.
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
            found.add(node.module)
    return {
        ".".join(part.split(".")[:2])
        for part in found
        if part.split(".")[0] in _TOP_PACKAGES and "." in part
    }


def observed_dependents(
    config: Config, test_files: Iterable[Path], root: Path = REPO
) -> dict[str, tuple[str, ...]]:
    """For each scope, the OTHER scopes whose test modules import its source packages.

    The DERIVATION behind `Scope.dependents`. A hand-kept list of who depends on whom is a
    value, and values rot between maintenances while the selection they narrow stays quiet
    about it; this reads the imports that are actually there, so the guard that compares the
    two turns a new cross-scope import into a failing test rather than an unrun suite.

    A test no scope owns is skipped rather than recorded: the committed config is required
    to claim every tracked test module or match it under `always`, and an `always` path runs
    in EVERY scoped plan already, so it needs no edge to be reached.

    Import text, not resolved modules: a test that imports a scope's package is the thing
    being asked about, and a scope that reads another's data through a file or a fixture is
    NOT covered here. That is a known floor on this instrument, not a claim of completeness,
    and it is why the miss audit stays the gate on promoting selection to a gate.
    """
    owner_of = _test_owner_index(config)
    reaches = _source_reachability(config, root)
    found: dict[str, set[str]] = {scope.name: set() for scope in config.scopes}
    for file in test_files:
        relative = (file.relative_to(root) if file.is_absolute() else file).as_posix()
        owner = owner_of(relative)
        if owner is None:
            continue
        try:
            text = file.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            raise PlanError(f"cannot read {file}: {exc}") from None
        for module in imported_packages(text, relative):
            as_path = module.replace(".", "/") + "/"
            for scope in config.scopes:
                if not matches(as_path, scope.sources):
                    continue
                # The test has to run when the scope it imports changes AND when anything
                # that scope imports changes. The second half is the closure, and taking it
                # over SOURCES is what keeps it finite -- a direct test edge alone misses an
                # upstream package the test never names.
                for upstream in reaches[scope.name]:
                    if upstream != owner:
                        found[upstream].add(owner)
    return {name: tuple(sorted(names)) for name, names in found.items() if names}


def _source_reachability(config: Config, root: Path = REPO) -> dict[str, frozenset[str]]:
    """Scope -> every scope it imports, transitively, itself included.

    Read off the SOURCE files. A scope's tests exercise whatever its sources pull in, so a
    change two packages upstream can break them. Closing HERE, over a graph of packages, is
    what lets `_with_dependents` stay one hop at plan time: the same closure taken over the
    test edges instead does not compose and reaches the whole repository.
    """
    direct: dict[str, set[str]] = {scope.name: {scope.name} for scope in config.scopes}
    for scope in config.scopes:
        for pattern in scope.sources:
            base = root / pattern.rstrip("/")
            if not base.is_dir():
                continue
            for file in base.rglob("*.py"):
                text = file.read_text(encoding="utf-8", errors="ignore")
                for module in imported_packages(text, file.as_posix()):
                    as_path = module.replace(".", "/") + "/"
                    for other in config.scopes:
                        if other.name != scope.name and matches(as_path, other.sources):
                            direct[scope.name].add(other.name)
    closed = {name: set(names) for name, names in direct.items()}
    changed = True
    while changed:
        changed = False
        for name, names in closed.items():
            grown = set(names)
            for other in tuple(names):
                grown |= direct.get(other, set())
            if grown != names:
                closed[name] = grown
                changed = True
    return {name: frozenset(names) for name, names in closed.items()}


def _test_owner_index(config: Config) -> Callable[[str], str | None]:
    """The scope owning a test path, longest declared prefix winning."""

    def owner(path: str) -> str | None:
        best: tuple[int, str] | None = None
        for scope in config.scopes:
            for pattern in scope.tests:
                if matches(path, (pattern,)) and (best is None or len(pattern) > best[0]):
                    best = (len(pattern), scope.name)
        return best[1] if best else None

    return owner


def read_changes(base: str, head: str) -> tuple[Change, ...]:
    """`git diff --name-status`, refusing anything that did not run cleanly."""
    result = subprocess.run(
        ["git", "diff", "--name-status", "-M", f"{base}...{head}"],
        capture_output=True,
        text=True,
        cwd=REPO,
        check=False,
    )
    if result.returncode != 0:
        raise PlanError(f"git diff {base}...{head} failed: {result.stderr.strip()}")
    changes: list[Change] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        # A rename reports OLD and NEW; both are changed paths, and the old one is a delete.
        changes.extend(Change(fields[0], path) for path in fields[1:])
    return tuple(changes)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    args = parser.parse_args(argv)
    try:
        result = plan(read_changes(args.base, args.head), read_config(args.config))
    except PlanError as exc:
        print(f"UNKNOWN: {exc}", file=sys.stderr)
        return 3
    print(f"verdict={result.verdict}")
    print(f"reason={result.reason}")
    print(f"scopes={','.join(result.scopes)}")
    print(f"tests={' '.join(result.test_paths)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
