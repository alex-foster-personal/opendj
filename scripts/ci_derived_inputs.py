"""Which derived CI blocks a change can move, read off the changed files alone (issue #11975).

Three committed blocks are DERIVED from the whole tree and held to their derivation by a guard
that recomputes it: the scope `dependents` and `helper_carried_paths` in ci/test-scopes.yml, and
the index readers ci/pr-always-run.txt must list. Each guard reads every pytest input (about 28 s,
55 s and 14 s locally, up to 300 s on a loaded runner), so a PR head ranks them at the always-run
list's distance 2 at best, behind the hard cap (ci/test-budget.yml). That is how main went red on
derived-block drift five times on Tue 10 Nov 2026 while every PR that caused it was green.

This reads only the changed `.py` files, at the diff base and at HEAD, through the derivations' own
readers (`_consumed_scopes`, `_imported_names`, `_mentioned_scopes`, `_loads_code_by_name`,
`loaded_test_helpers`), and names each guard whose inputs moved:

- `dependents`: the scopes a file consumes, the `tests.*` helpers it imports or loads. A
  same-scope import moves nothing.
- `helper_carried_paths`: the repository modules, mentioned scopes and by-name loads of a file the
  committed closure lists, a global-helper candidate, or a file no scope owns; or an added or
  deleted file in a carried scope, which can turn an import that resolved to nothing into one that
  reaches it.
- index readers: the `scripts.*` imports and tree-listing calls of a file under tests/ or scripts/.

The selector ranks each guard named here at distance 0 (`select_tests(pinned=...)`), so the exact
derivation runs on that head and the cap cannot cut it. A change that moves none of them adds no
test. Nothing here is a second derivation: it only decides when to run the real one.

Regression lines:
  - [if] a test adds an import of another scope and dependents are not pinned [then] main goes red after merge, so fail, [else stop].
  - [if] a closure module adds an import and helper_carried_paths is not pinned [then] main goes red after merge, so fail, [else stop].
  - [if] a body-only edit pins any derived guard [then] every PR pays minutes for nothing, so fail, [else stop].
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from scripts.ci_plan import (
    CONFIG_PATH,
    REPO,
    Config,
    PlanError,
    Scope,
    _consumed_scopes,
    _owner_index,
    _source_reachability,
    _test_owner_index,
    matches,
    read_config,
)
from scripts.ci_plan_carry import _loads_code_by_name, _mentioned_scopes
from scripts.ci_plan_sources import (
    HelperLoads,
    _imported_names,
    imported_test_helpers,
    is_test_module,
    loaded_test_helpers,
)

SCOPES_FILE = CONFIG_PATH.relative_to(REPO).as_posix()
ALWAYS_RUN_FILE = "ci/pr-always-run.txt"
#: What a tree read needs to spell ((private path) holds
#: this to its detector's vocabulary): a `git ls-files`, or an os/glob/pathlib listing call.
LISTING_TOKENS: tuple[str, ...] = ("ls-files", "rglob", "glob", "walk", "iterdir", "listdir", "scandir")

ReadText = Callable[[str], str | None]


@dataclass(frozen=True)
class DerivedGuard:
    module: str
    block: str
    regenerate: str


DEPENDENTS = DerivedGuard(
    "(private path)",
    "scope dependents in ci/test-scopes.yml",
    "uv run --no-sync python -m scripts.pre_push_guards write-dependents && git add ci/test-scopes.yml",
)
CARRY_PATHS = DerivedGuard(
    "tests/scripts/test_ci_plan_carry_paths.py",
    "helper_carried_paths in ci/test-scopes.yml",
    "uv run --no-sync python -m scripts.pre_push_guards write-carry-paths && git add ci/test-scopes.yml",
)
INDEX_READERS = DerivedGuard(
    "(private path)",
    "index readers in ci/pr-always-run.txt",
    "add each module the guard names to ci/pr-always-run.txt, or excuse it in its KEPT_OFF_THE_LIST",
)
DERIVED_GUARDS: tuple[DerivedGuard, ...] = (DEPENDENTS, CARRY_PATHS, INDEX_READERS)


def derived_guards_for(
    changed: Iterable[str], base_text: ReadText, head_text: ReadText, config: Config, root: Path = REPO
) -> dict[DerivedGuard, str]:
    """Guard -> the first changed path that moved its derivation's input. A missing text is a file
    the side does not have (added or deleted). A file the readers cannot parse names every guard:
    an unread file is never a reason to skip one."""
    moved: dict[DerivedGuard, str] = {}
    owner = _owner_index(config)
    carried = set(config.helper_carried)
    edges = _Edges(config, root)
    for path in changed:
        if path == SCOPES_FILE:
            moved.setdefault(DEPENDENTS, path)
            moved.setdefault(CARRY_PATHS, path)
        if path == ALWAYS_RUN_FILE:
            moved.setdefault(INDEX_READERS, path)
        if not path.endswith(".py"):
            continue
        before, after = base_text(path), head_text(path)
        if before == after:
            continue
        try:
            if edges.moved(path, before, after):
                moved.setdefault(DEPENDENTS, path)
            if _in_carry_reach(path, before, after, config, owner, carried) and _carry_inputs(
                before, path, config, root
            ) != _carry_inputs(after, path, config, root):
                moved.setdefault(CARRY_PATHS, path)
            if path.startswith(("tests/", "scripts/")) and _index_read_inputs(before, path) != _index_read_inputs(
                after, path
            ):
                moved.setdefault(INDEX_READERS, path)
        except PlanError:
            for guard in DERIVED_GUARDS:
                moved.setdefault(guard, path)
    return {guard: moved[guard] for guard in DERIVED_GUARDS if guard in moved}


def pinned_derived_guards(root: Path, changed: Iterable[str], base: str) -> list[tuple[str, str]]:
    """(guard module, selection reason) for each derived block the diff from `base` to the checkout
    can move; the form `select_tests(pinned=...)` takes. A tree with no ci/test-scopes.yml (a
    synthetic selector fixture) has no derived blocks, so it pins nothing."""
    if not (root / SCOPES_FILE).is_file():
        return []
    config = read_config(root / SCOPES_FILE)
    moved = derived_guards_for(
        changed, lambda path: _text_at(root, base, path), lambda path: _file_text(root, path), config, root
    )
    return [
        (guard.module, f"derived-block guard ({guard.block}; `{path}` moved its input)")
        for guard, path in moved.items()
    ]


# ----- helpers -----
@dataclass(frozen=True)
class _Consumption:
    """What `observed_dependents` reads off one file: the scopes it consumes, and the `tests.*`
    helpers it imports or loads (the foreign-importer and placed-helper edges)."""

    scopes: frozenset[str]
    helpers: frozenset[str]
    loads: HelperLoads

    @classmethod
    def of(cls, text: str | None, where: str, config: Config) -> _Consumption:
        """A missing file reads as empty: it consumes and loads nothing."""
        text = text or ""
        scopes = frozenset(scope.name for scope in _consumed_scopes(text, where, config))
        return cls(scopes, frozenset(imported_test_helpers(text, where)), loaded_test_helpers(text, where))


class _Edges:
    """Whether one file's change moves the `dependents` blocks, read against the committed ones.

    Exact for the two shapes that are most of the traffic, an added consumption in a scope source
    and in a scope-owned test module: the new edges are known, and the committed blocks say whether
    they are there already. Every other shape (a dropped consumption, which another file may still
    carry, a helper import, a by-name load, a helper module or a file no scope owns) names the guard.
    `_source_reachability` (about 6 s) is read only when a test starts consuming its own scope."""

    def __init__(self, config: Config, root: Path) -> None:
        self.config, self.root = config, root
        self.committed = {scope.name: frozenset(scope.dependents) for scope in config.scopes}
        self.source_owner = _owner_index(config, sources_only=True)
        self.test_owner = _test_owner_index(config)
        self._reaches: dict[str, frozenset[str]] | None = None

    def moved(self, path: str, before: str | None, after: str | None) -> bool:
        old = _Consumption.of(before, path, self.config)
        new = _Consumption.of(after, path, self.config)
        if old == new:
            return False
        if source := self.source_owner(path):
            added = new.scopes - old.scopes - {source.name}
            if old.scopes - new.scopes - {source.name}:
                return True
            # S -> c makes every dependent of S, S's own tests included, a dependent of c; the
            # closure guard already holds c's dependents onto everything c reaches.
            consumers = self.committed[source.name] | {source.name}
            return any(not consumers - {c} <= self.committed[c] for c in added)
        test_owner = self.test_owner(path)
        if test_owner is None:
            always_test = matches(path, self.config.always) and is_test_module(path)
            return not (always_test and (old.helpers, old.loads) == (new.helpers, new.loads))
        if not is_test_module(path) or old.helpers != new.helpers or old.loads != new.loads:
            return True
        if old.scopes - new.scopes:
            return True
        added = new.scopes - old.scopes
        return any(test_owner not in self.committed[u] for c in added for u in self._upstreams(c, test_owner))

    def _upstreams(self, scope: str, owner: str) -> frozenset[str]:
        """The scopes a test consuming `scope` becomes a dependent of: every scope it reaches but
        its own. Another scope's committed dependents already close over its reach (the closure
        guard), so only a test's own scope needs the source graph."""
        if scope != owner:
            return frozenset({scope})
        if self._reaches is None:
            self._reaches = _source_reachability(self.config, self.root)
        return self._reaches[scope] - {owner}


def _in_carry_reach(
    path: str,
    before: str | None,
    after: str | None,
    config: Config,
    owner: Callable[[str], Scope | None],
    carried: set[str],
) -> bool:
    """A file whose imports can move `helper_carried_paths`: one the committed closure lists, a
    global-helper candidate, one no scope owns (the closure walks through it), or an added or
    deleted file in a carried scope."""
    scope = owner(path)
    if matches(path, config.helper_carried_paths or ()) or scope is None or _is_helper_candidate(path, config):
        return True
    return (before is None or after is None) and scope.name in carried


def _is_helper_candidate(path: str, config: Config) -> bool:
    """Any conftest, or an `always`-matched non-test module: what `_global_helpers` may yield."""
    return path.endswith("conftest.py") or (matches(path, config.always) and not is_test_module(path))


def _carry_inputs(text: str | None, where: str, config: Config, root: Path) -> tuple[object, ...]:
    """What `helper_carried_paths` reads off one closure file: the repository modules it imports,
    the scopes it mentions, and, when it loads code by name, its whole text (only the full
    derivation follows such a load)."""
    if text is None:
        return ()
    repo_names = tuple(sorted(n for n in _imported_names(text, where) if _is_repository_name(n, root)))
    loads = text if _loads_code_by_name(text, where) else None
    return repo_names, tuple(sorted(_mentioned_scopes(config, text, where))), loads


def _is_repository_name(name: str, root: Path) -> bool:
    top = name.split(".", maxsplit=1)[0]
    return bool(top) and ((root / top).is_dir() or (root / f"{top}.py").is_file())


def _index_read_inputs(text: str | None, where: str) -> tuple[object, ...]:
    """What the index-reader detector needs to find a module: its `scripts.*` imports (a helper
    that reads the tree) and how often it spells a listing call. A missing file reads as empty, so
    a new module that spells neither moves nothing."""
    text = text or ""
    scripts = tuple(sorted(n for n in _imported_names(text, where) if n.split(".")[0] == "scripts"))
    return scripts, tuple(text.count(token) for token in LISTING_TOKENS)


def _text_at(root: Path, commit: str, path: str) -> str | None:
    """The file at `commit`, or None when it has none there; any other git failure raises."""
    result = subprocess.run(["git", "cat-file", "-e", f"{commit}:{path}"], cwd=root, capture_output=True, check=False)
    if result.returncode != 0:
        return None
    return subprocess.run(
        ["git", "show", f"{commit}:{path}"], cwd=root, capture_output=True, text=True, encoding="utf-8", check=True
    ).stdout


def _file_text(root: Path, path: str) -> str | None:
    file = root / path
    return file.read_text(encoding="utf-8") if file.is_file() else None
