"""The import-graph speedups in scripts/affected_tests.py: statement-only walk and import cache.

Measured Fri 16 Oct 2026 on bifrost1: `build_graph` took 18.6 s warm, 10.7 s of it in
`ast.walk` visiting 4.2M nodes, almost all expressions, to find import statements that
can only ever sit in a statement list. Every call rebuilt the whole graph from nothing.

Both fixes are only worth having if the graph they return is the graph the slow path
returned, so the tests below pin equivalence and staleness rather than speed.

Regression lines:
  - [if] the statement-only walk misses an import the full `ast.walk` finds, at any
    nesting a statement can have [then] fail, [else stop].
  - [if] an edited import does not show up on the very next call [then] fail, [else stop].
  - [if] an unchanged file is re-extracted instead of served from the cache [then] fail,
    [else stop]. Proven by a planted cache entry appearing in the graph.
  - [if] a cache entry is served after its file's content changed [then] fail, [else stop].
  - [if] a cache written by a different extractor version is served [then] fail, [else stop].
  - [if] an unreadable cache aborts the graph or is used silently [then] fail, [else stop].
  - [if] a cache entry of the wrong shape is coerced instead of rebuilt [then] fail,
    [else stop].
  - [if] a failed cache write stops the graph on a read-only tree [then] fail, [else stop].
  - [if] graph paths carry the host separator instead of `/` [then] fail, [else stop].
"""

from __future__ import annotations

import ast
import json
import textwrap
from pathlib import Path

import pytest

from scripts.affected_tests import (
    CACHE_RELATIVE,
    _imports_in_source,
    build_graph,
)
from scripts.ci_test_selection_rules import Graph

pytestmark = pytest.mark.requirement("OPS-16")


def _tree(root: Path, files: dict[str, str]) -> None:
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")


def _imports_by_full_walk(source: str) -> set[str]:
    """The pre-Fri 16 Oct 2026 extractor for absolute imports, kept as the reference."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


#: One import at every place a statement can nest. Each name is distinct, so a walk that
#: skips one nesting loses exactly that name and the assertion says which.
EVERY_NESTING = textwrap.dedent(
    """
    import top_level
    from top_from import name_a
    if cond:
        import in_if
    elif other:
        import in_elif
    else:
        import in_else
    for item in items:
        import in_for
    else:
        import in_for_else
    while cond:
        import in_while
    else:
        import in_while_else
    try:
        import in_try
    except ImportError:
        import in_except
    else:
        import in_try_else
    finally:
        import in_finally
    try:
        pass
    except* ValueError:
        import in_except_star
    with ctx:
        import in_with
    def function():
        import in_def
        def inner():
            import in_nested_def
    async def coroutine():
        import in_async_def
        async with ctx:
            import in_async_with
        async for item in items:
            import in_async_for
    class Klass:
        import in_class
        def method(self):
            import in_method
    match value:
        case 1:
            import in_match_case
        case _:
            from in_match_default import name_b
    @decorator
    def decorated():
        import in_decorated
    """
)


def test_statement_walk_finds_every_import_the_full_walk_finds():
    """[if] the statement-only walk misses an import at any nesting [then] fail, [else stop]."""
    expected = _imports_by_full_walk(EVERY_NESTING)
    assert len(expected) == 27, "control: the reference must see every planted import"
    assert _imports_in_source(EVERY_NESTING, Path("pkg/mod.py")) == expected


def test_statement_walk_resolves_relative_imports_like_before():
    source = "from . import sibling\nfrom ..up import thing\nfrom .near import x\n"
    assert _imports_in_source(source, Path("apps/pkg/mod.py")) == {
        "apps.pkg",
        "apps.pkg.sibling",
        "apps.up",
        "apps.up.thing",
        "apps.pkg.near",
        "apps.pkg.near.x",
    }


def _base_tree(root: Path) -> None:
    _tree(
        root,
        {
            "apps/__init__.py": "",
            "apps/engine.py": "VALUE = 1\n",
            "apps/other.py": "OTHER = 2\n",
            "tests/test_engine.py": "from apps.engine import VALUE\n",
        },
    )


def test_a_changed_import_shows_up_on_the_next_call(tmp_path):
    """[if] an edited import is not in the next graph [then] fail, [else stop]."""
    _base_tree(tmp_path)
    importers, _ = build_graph(tmp_path)
    assert (tmp_path / CACHE_RELATIVE).is_file(), "control: the first call must write a cache"
    assert "tests.test_engine" not in importers.get("apps.other", set())

    _tree(tmp_path, {"tests/test_engine.py": "from apps.other import OTHER\n"})
    importers, _ = build_graph(tmp_path)
    assert "tests.test_engine" in importers["apps.other"]
    assert "tests.test_engine" not in importers.get("apps.engine", set())


def _plant(root: Path, relative: str, imports: list[str]) -> None:
    cache_path = root / CACHE_RELATIVE
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    cache["files"][relative][1] = imports
    cache_path.write_text(json.dumps(cache), encoding="utf-8")


def test_an_unchanged_file_is_served_from_the_cache(tmp_path):
    """[if] an unchanged file is re-extracted rather than read from cache [then] fail, [else stop].

    The planted import exists only in the cache, so it can reach the graph only if the
    cache entry was served instead of the file being parsed again.
    """
    _base_tree(tmp_path)
    build_graph(tmp_path)
    _plant(tmp_path, "tests/test_engine.py", ["apps.other"])
    importers, _ = build_graph(tmp_path)
    assert "tests.test_engine" in importers["apps.other"]


def test_a_cache_entry_is_not_served_after_its_file_changes(tmp_path):
    """[if] a stale entry is served after a content change [then] fail, [else stop].

    Same planted entry as the served-from-cache test, so the only difference between the
    two outcomes is the file's content.
    """
    _base_tree(tmp_path)
    build_graph(tmp_path)
    _plant(tmp_path, "tests/test_engine.py", ["apps.other"])
    _tree(tmp_path, {"tests/test_engine.py": "from apps.engine import VALUE  # edited\n"})
    importers, _ = build_graph(tmp_path)
    assert "tests.test_engine" not in importers.get("apps.other", set())
    assert "tests.test_engine" in importers["apps.engine"]


def test_a_cache_from_another_extractor_version_is_not_served(tmp_path):
    _base_tree(tmp_path)
    build_graph(tmp_path)
    _plant(tmp_path, "tests/test_engine.py", ["apps.other"])
    cache_path = tmp_path / CACHE_RELATIVE
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    cache["extractor"] = "some-older-extractor"
    cache_path.write_text(json.dumps(cache), encoding="utf-8")
    importers, _ = build_graph(tmp_path)
    assert "tests.test_engine" not in importers.get("apps.other", set())


def test_an_unreadable_cache_rebuilds_loudly(tmp_path, capsys):
    """[if] a corrupt cache aborts the graph or is used without a word [then] fail, [else stop]."""
    _base_tree(tmp_path)
    build_graph(tmp_path)
    (tmp_path / CACHE_RELATIVE).write_text("{not json", encoding="utf-8")
    importers, _ = build_graph(tmp_path)
    assert "tests.test_engine" in importers["apps.engine"]
    assert "rebuilding" in capsys.readouterr().err
    json.loads((tmp_path / CACHE_RELATIVE).read_text(encoding="utf-8"))


def test_a_cache_entry_with_string_imports_rebuilds_loudly(tmp_path, capsys):
    """If a malformed entry were coerced then a string would become a list of characters."""
    root = tmp_path
    _base_tree(root)
    build_graph(root)
    cache_path = root / CACHE_RELATIVE
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    relative = next(iter(cache["files"]))
    cache["files"][relative][1] = "apps.engine"
    cache_path.write_text(json.dumps(cache), encoding="utf-8")
    capsys.readouterr()
    build_graph(root)
    assert "malformed" in capsys.readouterr().err


def test_an_unwritable_cache_still_builds_the_graph(tmp_path, capsys, monkeypatch):
    """If a read-only tree raised on the cache write then graphing it would fail."""
    root = tmp_path
    _base_tree(root)

    def refuse(*_args: object) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr("scripts.affected_tests._write_cache", refuse)
    importers, module_of = build_graph(root)
    assert module_of
    assert "not written" in capsys.readouterr().err


def test_a_missing_cache_is_silent(tmp_path, capsys):
    """Overshoot control: a first run has no cache, and that is not worth a warning."""
    _base_tree(tmp_path)
    build_graph(tmp_path)
    assert capsys.readouterr().err == ""


def test_selection_graph_paths_use_forward_slashes(tmp_path):
    """[if] graph paths carry the host separator [then] fail, [else stop].

    On Windows `str(Path)` yields backslashes, so every POSIX path git reports was
    UNKNOWN to the selector there, and every selection fell back to the full suite.
    """
    _base_tree(tmp_path)
    graph = Graph.load(tmp_path)
    assert graph.path_of["tests.test_engine"] == "tests/test_engine.py"
