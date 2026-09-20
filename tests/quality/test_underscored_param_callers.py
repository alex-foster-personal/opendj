"""A parameter renamed for a lint rule must not strand its keyword callers.

Ruff's ARG rules (unused argument) are satisfied by prefixing a parameter with
an underscore. That is correct for a private helper and WRONG for a public
signature: the parameter's name IS the interface, so renaming `state` to
`_state` turns every `f(state=...)` call site into a TypeError at run time,
which nothing catches until that code path executes in production.

Commit 17c7e99da (PR #2534, "pay down ruff lint debt") did exactly this to at
least ten public parameters. Measured on Sat 19 Sep 2026, the damage that had
reached main:

  - `apps.sets.record.status`'s `state` -> 500 on every GET /api/v1/sets/recorder,
    which the performance page polls, so the e2e gate failed on every PR in the
    repo, in all four of its steps.
  - `RescueStore.list_entries`'s `now_ms` -> TypeError from two call sites in
    its own module, failing all 11 rescue API tests.
  - `apps.sync.analysis_writeback.run_undo`'s `rb_db_path` -> `apply_analysis
    --undo` raised TypeError in production.
  - plus `compute_features`, two `live_run`s, `compare`, `run_ratings_apply`,
    `assert_merge_safe` and `bundle_remote_size`.

None of them was caught by review, by ruff, or by mypy: the call sites are
keyword arguments, and a renamed parameter is a valid signature.

So this test walks every function in `apps/` that has a leading-underscore
parameter, and every call in `apps/` and `tests/` that passes that parameter's
bare name as a keyword to a function of that name. A match is either a real
breakage or a name collision the reader must look at; both deserve a line here.

Regression lines:
  - if a public parameter is underscored while a caller still passes its bare
    name then that call raises TypeError when it runs, so broken
  - if this test reports zero while a known-broken pair exists then the walk
    stopped matching and is protecting nothing, so broken
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ROOTS = ("apps", "tests")


def _underscored_params() -> dict[str, set[str]]:
    """Map function name -> bare names of its leading-underscore parameters."""
    found: dict[str, set[str]] = {}
    for path in (REPO / "apps").rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            args = node.args
            for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
                name = arg.arg
                if name.startswith("_") and not name.startswith("__") and len(name) > 1:
                    found.setdefault(node.name, set()).add(name[1:])
    return found


def _stranded_callers() -> list[str]:
    params = _underscored_params()
    hits: set[str] = set()
    for root in ROOTS:
        for path in (REPO / root).rglob("*.py"):
            try:
                tree = ast.parse(path.read_text(errors="ignore"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if isinstance(func, ast.Attribute):
                    name = func.attr
                elif isinstance(func, ast.Name):
                    name = func.id
                else:
                    continue
                for keyword in node.keywords:
                    if keyword.arg in params.get(name, set()):
                        rel = path.relative_to(REPO)
                        hits.add(f"{rel}:{node.lineno} {name}({keyword.arg}=...) but the def takes _{keyword.arg}")
    return sorted(hits)


def test_no_caller_passes_the_bare_name_of_an_underscored_parameter() -> None:
    """[if] a caller passes a keyword the def underscored [then ⛔️] broken"""
    stranded = _stranded_callers()
    assert not stranded, (
        "if a caller passes the bare name of a parameter its definition "
        "underscored then that call raises TypeError when it runs, and no "
        "linter or type checker sees it - broken:\n  " + "\n  ".join(stranded)
    )


def test_the_walk_actually_finds_a_known_bad_pair() -> None:
    """[if] the walk reports zero on a planted breakage [then ⛔️] broken.

    Without this, a scan that silently stopped matching (an AST shape it no
    longer recognises, a root that moved) would report a clean repo forever.
    The planted pair is the real shape of the `record.status` bug.
    """
    source = (
        "def status(*, _state=None):\n"
        "    return 1\n"
        "\n"
        "def caller():\n"
        "    return status(state=object())\n"
    )
    tree = ast.parse(source)
    underscored: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]:
                if arg.arg.startswith("_") and len(arg.arg) > 1:
                    underscored.add(arg.arg[1:])
    passed = {
        keyword.arg
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
    }
    assert underscored & passed == {"state"}, (
        "if the detection shape no longer matches a known-broken pair then "
        "the repo-wide test above is protecting nothing - broken"
    )
