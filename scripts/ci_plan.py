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
SUPPORTED_CONFIG_VERSIONS = frozenset({1})
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
# NO HARMLESS CLASS SURVIVES. A suffix whitelist decided the unknown file type unsafely; a
# source-root whitelist decided the unknown location the same way; a harmless-suffix and a
# harmless-location BLACKLIST both turned out to be claims this repository refutes. Measured
# rather than reasoned: 24 prose files are named by a literal path inside a test module
# (`docs/architecture.md`, `.planning/REQUIREMENTS.md`, `AGENTS.md`), six more test modules
# GLOB for prose, and `tests/scripts/test_oss_tip_audit_source.py` audits the PATHNAME of
# every tracked file in the index, so adding or renaming any file at all is an input to it.
# A derived list of test-read documentation cannot be complete either, because a literal-path
# scan cannot see a glob or an index walk, and an incomplete list under-selects. Sol's P1 on
# #3339.
#
# The cost is stated plainly rather than hidden: SKIP_PYTEST is now unreachable, so the
# planner narrows nothing at all on this repository. That is the honest form of round 5's
# result, not a new one -- selection here was already 0% SCOPED -- and a planner that runs
# everything is merely useless, where one that skips a suite it should have run is wrong.


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
        else:
            unclaimed_source.append(path)
    if unclaimed_source:
        return Plan(
            Verdict.FULL,
            (),
            (),
            f"a source path no scope claims: {unclaimed_source[0]}",
        )
    if not hit:
        # UNREACHABLE by construction, and deliberately loud rather than deleted. Every path
        # now either hits a scope, matches `always`, or is unclaimed source that returned FULL
        # above, so there is no longer any way to earn SKIP_PYTEST: nothing in this repository
        # can be shown harmless (see the classifier note above). The member stays on `Verdict`
        # because round 6b imports it, and reaching here would mean the classification above
        # let a path through unclassified, which must fail rather than report a skip.
        raise PlanError(
            "no changed path was classified, which should be impossible; "
            f"refusing to report SKIP_PYTEST for {paths[:3]}"
        )

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
    # Every key REQUIRED and typed. `raw.get(key) or ()` turned a misspelled `full_triggers`
    # into no full triggers at all, which is a safety rule deleted by a typo and a narrower
    # plan reported as a correct one. An unknown schema version is refused for the same
    # reason: a newer config may mean something this planner would silently misread.
    # Sol's P1 on #3339.
    unknown = sorted(set(raw) - {"version", "full_triggers", "always", "scopes"})
    if unknown:
        raise PlanError(f"{path} has unknown key(s): {', '.join(unknown)}")
    if raw.get("version") not in SUPPORTED_CONFIG_VERSIONS:
        raise PlanError(
            f"{path} declares version {raw.get('version')!r}; "
            f"this planner reads {sorted(SUPPORTED_CONFIG_VERSIONS)}"
        )
    for key in ("full_triggers", "always", "scopes"):
        if not isinstance(raw.get(key), list):
            raise PlanError(f"{path} is missing a list `{key}`, or it is not a list")
    scopes = raw["scopes"]
    if not scopes:
        raise PlanError(f"{path} declares no scopes")
    for entry in scopes:
        if not isinstance(entry, dict):
            raise PlanError(f"{path} has a scope that is not a mapping: {entry!r}")
        missing = [k for k in ("name", "sources", "tests") if k not in entry]
        if missing:
            raise PlanError(f"{path}: scope {entry.get('name', entry)!r} lacks {missing}")
        extra = sorted(set(entry) - {"name", "sources", "tests", "dependents"})
        if extra:
            raise PlanError(f"{path}: scope {entry['name']!r} has unknown key(s): {extra}")
    return Config(
        tuple(raw["full_triggers"]),
        tuple(raw["always"]),
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


def _read_source(path: Path, where: str) -> str:
    """Decode strictly. A lenient decode deletes the bytes it cannot read and hands back a
    SHORTER file, so an import can vanish and the derivation gets quietly smaller while both
    it and the guard checking it agree on the same corrupted text. Same class as the parse
    failure below, and it was left behind when that one was fixed. Sol's P1 on #3339."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise PlanError(f"cannot decode {where} as utf-8: {exc}") from None
    except OSError as exc:
        raise PlanError(f"cannot read {where}: {exc}") from None


def is_test_module(path: str) -> bool:
    """pytest's own two shapes. Discovery and the completeness invariant have to agree on
    this: they used different predicates, so a `foo_test.py` could satisfy the ownership
    check while never being scanned for the imports that put its suite in a scoped plan.
    Sol's P1 on #3339."""
    name = path.rsplit("/", 1)[-1]
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def test_modules(root: Path) -> list[Path]:
    """Every module pytest will COLLECT under `root`, by the one predicate above.

    Collection is the question the completeness invariant asks. It is NOT the question the
    dependency derivation asks; see `pytest_inputs`."""
    return sorted(p for p in root.rglob("*.py") if is_test_module(p.as_posix()))


def pytest_inputs(root: Path) -> list[Path]:
    """Every Python file under `root`, collected or not.

    `conftest.py` runs automatically and fixture and helper modules are imported by the tests
    that use them, so their imports are the suite's dependencies just as much as a test
    module's. Feeding only collected modules to the derivation loses an edge whenever a helper
    reaches a scope its own tests never name: 24 support modules in this repository import a
    scope other than the one that owns them. Sol's P1 on #3339."""
    return sorted(root.rglob("*.py"))


def _relative_base(where: str, level: int) -> str:
    """The absolute package a `from ..x import y` in `where` refers to.

    Discarding relative imports outright is under-selection with extra steps: `from ..
    engine_core import X` in an `apps/open_dj/` module is a cross-scope edge, and dropping it
    leaves that suite out of a SCOPED plan when engine_core changes. Sol's P1 on #3339.

    A level that climbs past the repository root cannot be resolved, and unresolvable is not
    the same as absent, so it RAISES. Today's tree never goes deeper than level 2 and no
    relative import crosses a scope, which is why this was invisible rather than harmless.
    """
    parts = where.removesuffix(".py").split("/")[:-1]
    if level - 1 > len(parts):
        raise PlanError(f"relative import in {where} climbs past the repository root")
    kept = parts[: len(parts) - (level - 1)]
    return ".".join(kept)


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
        elif isinstance(node, ast.ImportFrom):
            base = _relative_base(where, node.level) if node.level else ""
            module = f"{base}.{node.module}" if base and node.module else (base or node.module)
            if not module:
                continue
            # `from apps import engine_core` names the package in the ALIAS, not the module.
            found.update(f"{module}.{alias.name}" for alias in node.names)
            found.add(module)
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
            # THREE cases here, and an earlier version conflated the first two on an
            # argument that is wrong for helpers. An always-matched TEST MODULE really does
            # run in every scoped plan, so it needs no edge. An always-matched HELPER does
            # NOT run: it is imported by suites that may be scope-owned and not always run,
            # so its imports are THEIR dependency. `tests/conftest.py` is the worked case,
            # auto-loaded for the whole tree and importing `apps.analysis`. A full trigger on
            # the helper covers editing the helper, never a change to what it imports. Sol's
            # P1 on #3339, raised after I rebutted the same point once and was wrong.
            #
            # That dependency is NOT represented as an edge. A global helper's consumers are
            # the whole tree, so the honest statement is "changing this scope runs
            # everything", and `full_triggers` says exactly that in one line per scope
            # instead of roughly 1600 dependent entries. `helper_carried_scopes` derives the
            # set and a guard holds the config to it.
            if matches(relative, config.always):
                continue
            if imported_packages(_read_source(file, relative), relative):
                raise PlanError(
                    f"{relative} is claimed by no scope and matched by no `always` entry, "
                    "yet imports one; it carries a dependency this derivation cannot place. "
                    "Claim it in ci/test-scopes.yml rather than narrowing a plan without it"
                )
            continue
        for module in imported_packages(_read_source(file, relative), relative):
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


def helper_carried_scopes(config: Config, root: Path = REPO) -> frozenset[str]:
    """Scopes whose change must run EVERYTHING, because a GLOBAL pytest helper imports them.

    A helper matched by `always` is not a test and does not run; it is imported by suites
    across the tree, including scope-owned suites that are not always run. Its imports are
    therefore a dependency of those suites, and its consumers cannot be resolved from its own
    text, so the attribution is the widest honest one.

    Closed over the source graph for the same reason `observed_dependents` is: a helper that
    imports scope A also depends on everything A imports.
    """
    reaches = _source_reachability(config, root)
    owner_of = _test_owner_index(config)
    carried: set[str] = set()
    for file in pytest_inputs(root / "tests"):
        relative = (file.relative_to(root) if file.is_absolute() else file).as_posix()
        if owner_of(relative) is not None or not matches(relative, config.always):
            continue
        if is_test_module(relative):
            continue
        for module in imported_packages(_read_source(file, relative), relative):
            as_path = module.replace(".", "/") + "/"
            for scope in config.scopes:
                if matches(as_path, scope.sources):
                    carried |= reaches[scope.name]
    return frozenset(carried)


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
                where = file.relative_to(root).as_posix() if file.is_absolute() else file.as_posix()
                for module in imported_packages(_read_source(file, where), where):
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
