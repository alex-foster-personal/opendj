"""Turn a pull request's diff into a pytest plan: SKIP_PYTEST, SCOPED or FULL.

SMARTEST-CI round 5 (`(private path)`). This REPORTS a plan; nothing acts on it.
Selection becomes a gate only once the miss audit has produced a recall number, which it has
not, so the only safe way to be wrong here is to over-select, and every rule below that
cannot decide answers FULL.

Scope names double as merge-queue impacted-target names if the queue ever runs in
parallel mode, so that switch needs no renames.

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
import re
import subprocess
import sys
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import yaml

from scripts.ci_plan_sources import (
    PlanError,
    _read_source,
    imported_modules,
    imported_test_helpers,
    is_test_module,
    loaded_test_helpers,
    mentioned_strings,
)

REPO = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO / "ci" / "test-scopes.yml"
CONFIG_LISTS = {  # each schema version and the lists it requires; nothing else is accepted
    2: ("full_triggers", "helper_carried", "always", "scopes"),
    3: ("full_triggers", "helper_carried", "helper_carried_paths", "always", "scopes"),  # per-file carry
}
SUPPORTED_CONFIG_VERSIONS = frozenset(CONFIG_LISTS)
CAP_FRACTION = 0.6


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
    # `(private path)` fails when the two disagree, so a new import across a
    # scope boundary forces this file to move rather than silently narrowing the selection.
    dependents: tuple[str, ...] = ()
    # Strings a pytest input MENTIONS when it consumes this scope's sources. The import
    # reader sees Python only, so a scope whose sources are Svelte, TypeScript or JSON has
    # no importers and its consumers have to be found another way: a test that reads,
    # builds or globs `apps/webui/frontend` names that path in its text. Declaring this
    # is what makes such a scope derivable at all (`_dependencies_derivable`); a scope
    # without it stays FULL on every non-Python change, as before. Round 9.
    mentions: tuple[str, ...] = ()


@dataclass(frozen=True)
class Config:
    full_triggers: tuple[str, ...]
    always: tuple[str, ...]
    scopes: tuple[Scope, ...]
    # Scopes a GLOBAL pytest helper imports (or mentions), so a change to a path they OWN
    # runs everything. Named by scope rather than written as path triggers because
    # ownership is longest-prefix: `apps/webui/frontend/` is its own scope inside
    # `apps/webui/`, and a prefix trigger on the parent would swallow the child. Derived by
    # `helper_carried_scopes` and held to it by a guard. Round 9.
    helper_carried: tuple[str, ...] = ()
    # Round 22: inside a carried scope, the `.py` files a global helper's import closure reaches
    # (exact paths), or a scope's source prefix where that closure cannot be read off the imports.
    # A `.py` change outside it plans like its scope. `None` is round 19: every file is carried.
    helper_carried_paths: tuple[str, ...] | None = None


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
# GLOB for prose, and `(private path)` audits the PATHNAME of
# every tracked file in the index, so adding or renaming any file at all is an input to it.
# A derived list of test-read documentation cannot be complete either, because a literal-path
# scan cannot see a glob or an index walk, and an incomplete list under-selects. Sol's P1 on
# #3339.
#
# Round 10 keeps every word of that and still narrows: the prose trees are CLAIMED by a
# `prose` scope whose consumers the mention reader finds (the tests that spell `docs`,
# `specs`, ...), so a docs change runs those suites rather than nothing or everything.
#
# The cost is stated plainly rather than hidden: SKIP_PYTEST is now unreachable, so the
# planner narrows nothing at all on this repository. That is the honest form of round 5's
# result, not a new one -- selection here was already 0% SCOPED -- and a planner that runs
# everything is merely useless, where one that skips a suite it should have run is wrong.


def _classify_paths(paths: list[str], config: Config) -> tuple[set[str], list[str], list[str]]:
    """Every changed path into exactly one of three buckets: the scopes it hits, the paths no
    scope claims, and the claimed paths whose dependencies the closure cannot read.

    Lifted out of `plan` because that function sits on the C901 ceiling and this loop is the
    part with the branches. The three buckets are returned rather than acted on here, so the
    ORDER the verdicts are checked in stays visible in one place.
    """
    owner_of = _owner_index(config)
    hit: set[str] = set()
    unclaimed_source: list[str] = []
    unanalyzable: list[str] = []
    for path in paths:
        owner = owner_of(path)
        if owner is not None:
            hit.add(owner.name)
            if not _dependencies_derivable(path, owner):
                unanalyzable.append(path)
        elif matches(path, config.always):
            hit.add(_ALWAYS)
        else:
            unclaimed_source.append(path)
    return hit, unclaimed_source, unanalyzable


def _dependencies_derivable(path: str, owner: Scope) -> bool:
    """Whether a derivation can see who consumes this file.

    `_source_reachability` reads Python imports and nothing else, so a scope's TypeScript,
    Svelte, shell or JSON sources contribute NO edges. Owning the changed path is therefore
    not enough: a `.ts` file consumed across a scope boundary produces a SCOPED plan that
    omits the suites that consume it, which is under-selection with the scope config looking
    entirely correct. 2058 of this repository's 4646 claimed files are not Python, measured,
    so this is the common case rather than an edge. Sol's P1 on #3339.

    Deliberately a property of the INSTRUMENT, not a list of risky file types: it answers
    "can a derivation see this". Two derivations exist: the import reader, which sees any
    `.py`, and the mention reader (round 9), which sees every file of a scope that declares
    `mentions`, because that scope's consumers are found by the strings they name rather
    than the packages they import.
    """
    return path.endswith(".py") or bool(owner.mentions)


def _owner_index(config: Config, *, sources_only: bool = False) -> Callable[[str], Scope | None]:
    """The scope owning a path, longest declared prefix winning.

    ONE owner per path. Every scope whose prefix matched used to count, which made a scope
    nested inside another (`apps/webui/frontend/` inside `apps/webui/`) impossible: the
    parent claimed the child's files too, and the parent being helper-carried made every
    child change FULL. Longest prefix is the rule `_test_owner_index` already applied to
    tests; this applies it to sources as well. `sources_only` is the form the import graph
    uses, where a package path can only ever be a source.
    """
    by_length = sorted(
        (
            (len(pattern), pattern, scope)
            for scope in config.scopes
            for pattern in (scope.sources if sources_only else scope.sources + scope.tests)
        ),
        key=lambda item: -item[0],
    )

    def owner(path: str) -> Scope | None:
        for _, pattern, scope in by_length:
            if matches(path, (pattern,)):
                return scope
        return None

    return owner


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


def _helper_reaches(path: str, config: Config) -> bool:
    """Whether a global helper can reach this file of a carried scope. A non-Python file may be
    read as data, which no import reader sees, so it is always reached."""
    if config.helper_carried_paths is None or not path.endswith(".py"):
        return True
    return matches(path, config.helper_carried_paths)


def plan(changes: tuple[Change, ...], config: Config, cap_fraction: float = CAP_FRACTION) -> Plan:
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
    owner_of = _owner_index(config)
    carried = [
        p
        for p in paths
        if (o := owner_of(p)) is not None and o.name in config.helper_carried and _helper_reaches(p, config)
    ]
    if carried:
        return Plan(
            Verdict.FULL,
            (),
            (),
            f"a global pytest helper carries the scope owning: {carried[0]}",
        )

    by_name = {s.name: s for s in config.scopes}
    hit, unclaimed_source, unanalyzable = _classify_paths(paths, config)
    if unclaimed_source:
        return Plan(
            Verdict.FULL,
            (),
            (),
            f"a source path no scope claims: {unclaimed_source[0]}",
        )
    if unanalyzable:
        return Plan(
            Verdict.FULL,
            (),
            (),
            f"a claimed path whose dependencies cannot be derived: {unanalyzable[0]}",
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
    # Sol's P1 on #3339. Version 2 predates `helper_carried_paths` and reads as it always did, each
    # helper-carried scope carried whole (None); version 3 requires the list (Sol on #7495).
    version = raw.get("version")
    if version not in SUPPORTED_CONFIG_VERSIONS:
        raise PlanError(f"{path} declares version {version!r}; this planner reads {sorted(SUPPORTED_CONFIG_VERSIONS)}")
    lists = CONFIG_LISTS[version]
    unknown = sorted(set(raw) - {"version", *lists})
    if unknown:
        raise PlanError(f"{path} has unknown key(s) for version {version}: {', '.join(unknown)}")
    for key in lists:
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
        extra = sorted(set(entry) - {"name", "sources", "tests", "dependents", "mentions"})
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
                tuple(entry.get("mentions") or ()),
            )
            for entry in scopes
        ),
        tuple(raw["helper_carried"]),
        tuple(raw["helper_carried_paths"]) if "helper_carried_paths" in lists else None,
    )


def observed_dependents(config: Config, test_files: Iterable[Path], root: Path = REPO) -> dict[str, tuple[str, ...]]:
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
    inputs = {(file.relative_to(root) if file.is_absolute() else file).as_posix(): file for file in test_files}
    importers = _helper_importers(inputs, root)
    for relative, file in inputs.items():
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
            #
            # Round 19 places the helper whose importers are all placed TEST modules: their
            # owners are the suites to run, so it gets one edge per owner and carries nothing.
            if matches(relative, config.always):
                if not is_test_module(relative) and _consumed_only_by_placed_tests(
                    relative, importers[relative], owner_of, config
                ):
                    owners = {o for c in importers[relative] if (o := owner_of(c)) is not None}
                    for scope in _consumed_scopes(_read_source(file, relative), relative, config):
                        for upstream in reaches[scope.name]:
                            found[upstream] |= owners - {upstream}
                continue
            if _consumed_scopes(_read_source(file, relative), relative, config):
                raise PlanError(
                    f"{relative} is claimed by no scope and matched by no `always` entry, "
                    "yet consumes one; it carries a dependency this derivation cannot place. "
                    "Claim it in ci/test-scopes.yml rather than narrowing a plan without it"
                )
            continue
        for scope in _consumed_scopes(_read_source(file, relative), relative, config):
            # The test has to run when the scope it consumes changes AND when anything
            # that scope imports changes. The second half is the closure, and taking it
            # over SOURCES is what keeps it finite -- a direct test edge alone misses an
            # upstream package the test never names.
            for upstream in reaches[scope.name]:
                if upstream != owner:
                    found[upstream].add(owner)
        for upstream, foreign in _foreign_importer_edges(relative, file, owner, importers, owner_of, reaches, config):
            found[upstream] |= foreign
    return {name: tuple(sorted(names)) for name, names in found.items() if names}


def _foreign_importer_edges(
    relative: str,
    file: Path,
    owner: str,
    importers: dict[str, set[str]],
    owner_of: Callable[[str], str | None],
    reaches: dict[str, frozenset[str]],
    config: Config,
) -> Iterator[tuple[str, set[str]]]:
    """Round 27's miss: `tests/sync/conftest.py` re-exports fixtures from `tests/sets/conftest.py`,
    which imports `apps.sets`, and nothing under `tests/sync/` names that package. A scope-owned
    test file's imports are also a dependency of every OTHER scope whose files import it, directly
    or through other helpers, so each upstream of what it consumes gets those scopes as dependents.
    A session-wide load has no owner and adds none."""
    foreign = {
        o for consumer in _transitive_importers(importers, relative) if (o := owner_of(consumer)) not in (None, owner)
    }
    if not foreign:
        return
    for scope in _consumed_scopes(_read_source(file, relative), relative, config):
        for upstream in reaches[scope.name]:
            yield upstream, {name for name in foreign if name != upstream}


def _transitive_importers(importers: dict[str, set[str]], target: str) -> set[str]:
    found: set[str] = set()
    todo = list(importers[target])
    while todo:
        if (consumer := todo.pop()) not in found:
            found.add(consumer)
            todo.extend(importers.get(consumer, ()))
    return found


SESSION_LOAD = "<pytest_plugins>"  # a consumer no scope owns: a session-wide or unresolved load


def _helper_importers(inputs: dict[str, Path], root: Path) -> dict[str, set[str]]:
    """For each pytest input, the other inputs that load it: by import, or by `import_module` with a
    literal name. One that `pytest_plugins` names is loaded for the whole session, a load the reader
    cannot resolve may be any of them, and a computed path load may be any file its module names:
    each adds SESSION_LOAD, which no rule places. So does every helper the rootdir `conftest.py`
    (outside `tests/`, loaded for every session) loads or imports, and one a call that merely looks
    like a loader names."""
    importers: dict[str, set[str]] = {relative: set() for relative in inputs}
    sources = [(relative, file, relative) for relative, file in inputs.items()]
    if (rootdir_conftest := root / "conftest.py").is_file():
        sources.append(("conftest.py", rootdir_conftest, SESSION_LOAD))  # every session loads it
    unresolved = False
    path_targets: set[str] = set()
    by_folded: dict[str, list[str]] = defaultdict(list)
    for relative in inputs:
        by_folded[relative.casefold()].append(relative)
    for relative, file, consumer in sources:
        text = _read_source(file, relative)
        loads = loaded_test_helpers(text, relative)
        unresolved |= loads.unresolved
        path_targets |= loads.path_targets
        for target in _helper_files(imported_test_helpers(text, relative) | loads.by_call, importers):
            if target != relative:
                importers[target].add(consumer)
        for target in _helper_files(loads.by_plugins | loads.lookalike, importers):
            importers[target].add(SESSION_LOAD)
        named = imported_test_helpers(text, relative) | loads.by_call | loads.by_plugins | loads.lookalike
        for target in _case_variants(named, by_folded):
            importers[target].add(SESSION_LOAD)
    for target, consumers in importers.items():
        name = target.rsplit("/", 1)[-1].casefold()
        if unresolved or name in path_targets or name.removesuffix(".py") in path_targets:
            consumers.add(SESSION_LOAD)
    return importers


def _helper_files(names: Iterable[str], importers: dict[str, set[str]]) -> list[str]:
    """The pytest inputs that dotted `tests.*` names are, skipping names no input is. A wildcard
    import (`from tests.pkg import *`, read as `tests.pkg.*`) imports every submodule the package's
    `__all__` lists (Sol on #7121), so it names every module under the package, a superset of what
    `__all__` can list. No test module in the tree imports one (Fri 30 Oct 2026)."""
    found: list[str] = []
    for name in names:
        if name.endswith(".*"):
            package = name.removesuffix("*").replace(".", "/")
            found += [target for target in importers if target.startswith(package)]
        elif (target := name.replace(".", "/") + ".py") in importers:
            found.append(target)
    return found


def _case_variants(names: Iterable[str], by_folded: Mapping[str, list[str]]) -> list[str]:
    """The pytest inputs a name reaches only in another case: `run_path(r"Tests\\Rig.py")` runs
    `tests/rig.py` on a case-insensitive (Windows) checkout and fails elsewhere, so the load depends
    on the platform and no scope can own it."""
    spelled = {name.replace(".", "/") + ".py" for name in names}
    return [target for path in spelled for target in by_folded.get(path.casefold(), []) if target not in spelled]


_COLLECTION_LOADED = {"conftest.py", "__init__.py"}  # pytest runs these for every test beneath them


def _consumed_only_by_placed_tests(
    helper: str, consumers: set[str], owner_of: Callable[[str], str | None], config: Config
) -> bool:
    """True when every module importing `helper` is a test that a scope owns or `always` runs,
    so its imports are a dependency of known suites: an edge to each owner, none for an always-run
    test. Empty consumers is NOT that: a helper reached through `pytest_plugins`, a fixture name or
    nothing at all cannot be placed, nor can a `conftest.py` or a package `__init__.py`, which
    pytest runs while collecting every test beneath it rather than for an importer, or one a
    helper imports, whose own consumers this does not follow. Unplaceable stays widest."""
    if helper.rsplit("/", 1)[-1] in _COLLECTION_LOADED or not consumers:
        return False
    return all(is_test_module(c) and (owner_of(c) is not None or matches(c, config.always)) for c in consumers)


def _consumed_scopes(text: str, where: str, config: Config) -> list[Scope]:
    """The scopes one pytest input depends on: those it IMPORTS, read with the AST, plus
    those it MENTIONS, for scopes that declare the strings their consumers name.

    Both readers feed one edge set so the guard on `dependents` covers both. A mention is a
    substring test over the module's string LITERALS (comments and docstrings excluded, see
    `mentioned_strings`), and that floor is stated rather than hidden: a test that reaches a
    frontend file through a path it never spells out is invisible here, exactly as a test
    reading another scope's data file is invisible to the import reader. The miss audit
    (round 7) is what measures that floor on real runs.
    """
    source_owner = _owner_index(config, sources_only=True)
    consumed: dict[str, Scope] = {}
    for module in imported_modules(text, where):
        scope = source_owner(module.replace(".", "/") + "/")
        if scope is not None:
            consumed[scope.name] = scope
    literals = mentioned_strings(text, where)
    for scope in config.scopes:
        if any(needle in literal for needle in scope.mentions for literal in literals):
            consumed[scope.name] = scope
    return [consumed[name] for name in sorted(consumed)]


def _source_reachability(config: Config, root: Path = REPO) -> dict[str, frozenset[str]]:
    """Scope -> every scope it imports, transitively, itself included.

    Read off the SOURCE files. A scope's tests exercise whatever its sources pull in, so a
    change two packages upstream can break them. Closing HERE, over a graph of packages, is
    what lets `_with_dependents` stay one hop at plan time: the same closure taken over the
    test edges instead does not compose and reaches the whole repository.
    """
    source_owner = _owner_index(config, sources_only=True)
    direct: dict[str, set[str]] = {scope.name: {scope.name} for scope in config.scopes}
    for scope in config.scopes:
        for pattern in scope.sources:
            base = root / pattern.rstrip("/")
            if not base.is_dir():
                continue
            for file in base.rglob("*.py"):
                where = file.relative_to(root).as_posix() if file.is_absolute() else file.as_posix()
                # A file inside a NESTED scope's prefix belongs to that scope, not to this
                # one, so its imports are attributed there; the same longest-prefix rule
                # `_owner_index` applies at plan time, so the two cannot disagree.
                if source_owner(where) is not scope:
                    continue
                for module in imported_modules(_read_source(file, where), where):
                    other = source_owner(module.replace(".", "/") + "/")
                    if other is not None and other.name != scope.name:
                        direct[scope.name].add(other.name)
    closed = {name: set(names) for name, names in direct.items()}
    changed = True
    while changed:
        changed = False
        for name, names in closed.items():
            grown = set(names)
            for reached in tuple(names):
                grown |= direct.get(reached, set())
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
